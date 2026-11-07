"""Screen a staged point before it may enter the trusted shard.

Memory poisoning is treated as an attack. Every check runs and every failed
check is recorded, so the quarantine entry explains itself. Checks:

  provenance  unregistered device, key differs from the key this device pinned,
              missing/invalid Ed25519 signature
  trust       source trust below MIN_TRUST: this device's *effective* trust for
              the signer (engram.reputation), the operator's static score
              lowered by that signer's provable misbehavior
  routing     a PRIVATE memory showed up on the wire
  semantics   contradicts a corroborated local memory on the same subject, or is
              a near-copy (dense cosine >= SEMANTIC_THRESHOLD) of a
              corroborated memory filed under a different subject -- the
              "rename the subject to dodge the check" evasion
  nli         a *paraphrase* of the evasion: a new subject whose text an on-device
              NLI cross-encoder reads as being about a corroborated memory's subject
              (topic gate) and contradicting it (cosine >= NLI_FLOOR,
              P(topic) >= nli.TOPIC_THRESHOLD, P(contradiction) >= NLI_THRESHOLD)
"""
from dataclasses import dataclass, field

from qdrant_edge import EdgeShard, Filter

from . import nli
from . import shard as shard_ops
from .schema import PRIVATE, Memory

MIN_TRUST = 0.5
CORROBORATION_THRESHOLD = 2
# Calibrated on the seed data with bge-small: closest legitimate cross-subject
# pair = 0.849, near-copy evasion = 0.90-0.91, paraphrased evasion = 0.68-0.76
# (below threshold; caught by the NLI step instead).
SEMANTIC_THRESHOLD = 0.88
# NLI runs only on corroborated neighbors at or above this cosine, top NLI_MAX_PAIRS of them,
# and only for memories that passed every other check: unsigned or untrusted junk cannot buy model time.
NLI_FLOOR = 0.55
NLI_THRESHOLD = 0.90
NLI_MAX_PAIRS = 4


@dataclass
class Verdict:
    decision: str  # promote | corroborate | conflict | quarantine
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    same_value_ids: list[str] = field(default_factory=list)
    signed_ok: bool = False  # valid signature under the pinned key: this memory is evidence about its signer


def _brief(payload: dict, **extra) -> dict:
    return {
        "id": str(payload.get("id")),
        "subject": payload.get("subject"),
        "value": payload.get("value"),
        "corroborated_by": payload.get("corroborated_by", []),
        **extra,
    }


def _provenance_checks(memory: Memory, registry: dict, pinned_keys: dict,
                       trust_of: dict | None = None) -> tuple[list[str], list[dict], bool]:
    """Who sent it, is the signature theirs, and do we trust them.
    Returns (reasons, evidence, signed_ok); signed_ok means the pinned key proves the signer."""
    reasons, evidence = [], []
    if memory.sensitivity == PRIVATE:
        reasons.append("private_memory_on_the_wire")

    entry = registry.get(memory.source_device)
    pinned = pinned_keys.get(memory.source_device)
    if entry is None and pinned is None:
        reasons.append("unregistered_device")
        return reasons, evidence, False

    key = pinned or entry["public_key"]
    signed_ok = memory.verify(key)
    if not signed_ok:
        reasons.append("invalid_or_missing_signature")
        if entry is not None and entry["public_key"] != key and memory.verify(entry["public_key"]):
            reasons.append("registry_key_mismatch")
            evidence.append({"why": "signature matches the hub's current key for this device, "
                                    "not the key this device pinned on first contact"})
    trust = (trust_of or {}).get(memory.source_device, (entry or {}).get("trust_score", 0.0))
    if trust < MIN_TRUST:
        reasons.append(f"low_trust_source({trust:.2f}<{MIN_TRUST})")
    return reasons, evidence, signed_ok


def _corroborated(record) -> bool:
    return len(record.payload.get("corroborated_by", [])) >= CORROBORATION_THRESHOLD


def _semantic_evasions(memory: Memory, dense: list[float], trusted: EdgeShard) -> list:
    """Corroborated memories that say something different under another subject."""
    neighbors = shard_ops.dense_neighbors(
        trusted,
        dense,
        limit=5,
        filter=Filter(
            must=[shard_ops.match("trust_status", "verified")],
            must_not=[shard_ops.match("subject", memory.subject)],
        ),
        score_threshold=SEMANTIC_THRESHOLD,
    )
    return [n for n in neighbors if _corroborated(n) and n.payload["value"] != memory.value]


def _nli_contradictions(memory: Memory, dense: list[float], trusted: EdgeShard) -> list[tuple]:
    """Corroborated neighbors the NLI model reads as the same topic and contradicted. [(record, judgement)]"""
    near = shard_ops.dense_neighbors(
        trusted, dense, limit=NLI_MAX_PAIRS * 2,
        filter=Filter(must=[shard_ops.match("trust_status", "verified")]),
        score_threshold=NLI_FLOOR,
    )
    near = [n for n in near if _corroborated(n) and n.payload["value"] != memory.value][:NLI_MAX_PAIRS]
    if not near:
        return []
    judged = nli.judge(memory.text, [(n.payload["text"], n.payload["subject"]) for n in near])
    return [(n, j) for n, j in zip(near, judged)
            if j["contradiction"] is not None and j["contradiction"] >= NLI_THRESHOLD]


def _decide(reasons: list[str], evidence: list[dict], different: list, same_value: list,
            signed_ok: bool) -> Verdict:
    if reasons:
        return Verdict("quarantine", reasons, evidence, signed_ok=signed_ok)
    if different:
        return Verdict("conflict", evidence=[_brief(r.payload) for r in different], signed_ok=signed_ok)
    if same_value:
        return Verdict("corroborate", same_value_ids=[str(r.id) for r in same_value], signed_ok=signed_ok)
    return Verdict("promote", signed_ok=signed_ok)


def screen(memory: Memory, dense: list[float], registry: dict, pinned_keys: dict, trusted: EdgeShard,
           trust_of: dict | None = None) -> Verdict:
    reasons, evidence, signed_ok = _provenance_checks(memory, registry, pinned_keys, trust_of)

    same_subject = shard_ops.scroll_all(trusted, shard_ops.trusted_filter([shard_ops.match("subject", memory.subject)]))
    same_value = [r for r in same_subject if r.payload["value"] == memory.value]
    different = [r for r in same_subject if r.payload["value"] != memory.value]
    corroborated_conflicts = [r for r in different if _corroborated(r)]
    if corroborated_conflicts:
        reasons.append("contradicts_corroborated_memory")
        evidence += [_brief(r.payload, why="same subject, corroborated, different value") for r in corroborated_conflicts]

    evasions = _semantic_evasions(memory, dense, trusted)
    if evasions:
        reasons.append("semantic_conflict_with_corroborated_memory")
        evidence += [_brief(n.payload, why=f"cosine {n.score:.3f} under a different subject") for n in evasions]

    # A memory under a brand-new subject is the only shape the same-subject check cannot judge,
    # so that is where a paraphrased contradiction would hide. The model is the expensive check:
    # it runs last, and only for memories every cheaper check let through.
    if nli.ENABLED and not reasons and signed_ok and not same_subject:
        hits = _nli_contradictions(memory, dense, trusted)
        if hits:
            reasons.append("nli_contradiction_with_corroborated_memory")
            evidence += [_brief(n.payload, text=n.payload["text"],
                                why=f"NLI: about {n.payload['subject']} (P={j['topic']:.2f}), "
                                    f"contradicts it (P={j['contradiction']:.2f}), cosine {n.score:.3f}")
                         for n, j in hits]

    return _decide(reasons, evidence, different, same_value, signed_ok)
