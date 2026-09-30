"""Screen a staged point before it may enter the trusted shard.

Memory poisoning is treated as an attack. Every check runs and every failed
check is recorded, so the quarantine entry explains itself. Checks:

  provenance  unregistered device, key differs from the key this device pinned,
              missing/invalid Ed25519 signature
  trust       source trust below MIN_TRUST (static hub config)
  routing     a PRIVATE memory showed up on the wire
  semantics   contradicts a corroborated local memory on the same subject, or is
              a near-copy (dense cosine >= SEMANTIC_THRESHOLD) of a
              corroborated memory filed under a different subject -- the
              "rename the subject to dodge the check" evasion
"""
from dataclasses import dataclass, field

from qdrant_edge import EdgeShard, Filter

from . import shard as shard_ops
from .schema import PRIVATE, Memory

MIN_TRUST = 0.5
CORROBORATION_THRESHOLD = 2
# Calibrated on the seed data with bge-small: closest legitimate cross-subject
# pair = 0.849, near-copy evasion = 0.90-0.91, paraphrased evasion = 0.68-0.76
# (not caught -- see README limits).
SEMANTIC_THRESHOLD = 0.88


@dataclass
class Verdict:
    decision: str  # promote | corroborate | conflict | quarantine
    reasons: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    same_value_ids: list[str] = field(default_factory=list)


def _brief(payload: dict, **extra) -> dict:
    return {
        "id": str(payload.get("id")),
        "subject": payload.get("subject"),
        "value": payload.get("value"),
        "corroborated_by": payload.get("corroborated_by", []),
        **extra,
    }


def screen(memory: Memory, dense: list[float], registry: dict, pinned_keys: dict, trusted: EdgeShard) -> Verdict:
    reasons, evidence = [], []

    if memory.sensitivity == PRIVATE:
        reasons.append("private_memory_on_the_wire")

    entry = registry.get(memory.source_device)
    pinned = pinned_keys.get(memory.source_device)
    if entry is None and pinned is None:
        reasons.append("unregistered_device")
    else:
        key = pinned or entry["public_key"]
        if not memory.verify(key):
            reasons.append("invalid_or_missing_signature")
            if entry is not None and entry["public_key"] != key and memory.verify(entry["public_key"]):
                reasons.append("registry_key_mismatch")
                evidence.append({"why": "signature matches the hub's current key for this device, "
                                        "not the key this device pinned on first contact"})
        trust = (entry or {}).get("trust_score", 0.0)
        if trust < MIN_TRUST:
            reasons.append(f"low_trust_source({trust:.2f}<{MIN_TRUST})")

    same_subject = shard_ops.scroll_all(trusted, shard_ops.trusted_filter([shard_ops.match("subject", memory.subject)]))
    same_value = [r for r in same_subject if r.payload["value"] == memory.value]
    different = [r for r in same_subject if r.payload["value"] != memory.value]
    corroborated_conflicts = [
        r for r in different if len(r.payload.get("corroborated_by", [])) >= CORROBORATION_THRESHOLD
    ]
    if corroborated_conflicts:
        reasons.append("contradicts_corroborated_memory")
        evidence += [_brief(r.payload, why="same subject, corroborated, different value") for r in corroborated_conflicts]

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
    evasions = [
        n for n in neighbors
        if len(n.payload.get("corroborated_by", [])) >= CORROBORATION_THRESHOLD and n.payload["value"] != memory.value
    ]
    if evasions:
        reasons.append("semantic_conflict_with_corroborated_memory")
        evidence += [_brief(n.payload, why=f"cosine {n.score:.3f} under a different subject") for n in evasions]

    if reasons:
        return Verdict("quarantine", reasons, evidence)
    if different:
        return Verdict("conflict", evidence=[_brief(r.payload) for r in different])
    if same_value:
        return Verdict("corroborate", same_value_ids=[str(r.id) for r in same_value])
    return Verdict("promote")
