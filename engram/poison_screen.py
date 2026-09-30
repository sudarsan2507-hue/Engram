"""Screen points pulled into a staging shard before they are promoted to the
trusted shard. Memory poisoning is treated as an attack: unsigned, unregistered,
low-trust, or corroborated-contradicting points are quarantined, not merged."""
from qdrant_edge import EdgeShard

from . import shard as shard_ops
from .schema import Memory, TRUST_VERIFIED, PRIVATE

MIN_TRUST = 0.4
CORROBORATION_THRESHOLD = 2  # local memory is "corroborated" if >=2 distinct devices agree


def screen_one(memory: Memory, registry: dict, trusted_shard: EdgeShard) -> tuple[str, list[str]]:
    """Returns (decision, reasons). decision in {'promote', 'quarantine', 'conflict'}."""
    reasons = []

    if memory.sensitivity == PRIVATE:
        return "quarantine", ["private_memory_must_not_sync"]

    dev = registry.get(memory.source_device)
    if dev is None:
        return "quarantine", ["unregistered_device"]

    if not memory.verify(dev["public_key"]):
        return "quarantine", ["invalid_or_missing_signature"]

    if dev.get("trust_score", 0.0) < MIN_TRUST:
        reasons.append("low_trust_source")

    from qdrant_edge import ScrollRequest

    records, _ = trusted_shard.scroll(
        ScrollRequest(
            filter=shard_ops.eq_filter(subject=memory.subject, trust_status=TRUST_VERIFIED),
            limit=1000,
            with_payload=True,
        )
    )
    conflicting = [r for r in records if r.payload.get("value") != memory.value]
    corroborated_conflict = [
        r for r in conflicting if len(r.payload.get("corroborated_by", [])) >= CORROBORATION_THRESHOLD
    ]

    if corroborated_conflict:
        reasons.append("contradicts_corroborated_memory")

    if reasons:
        return "quarantine", reasons

    if conflicting:
        return "conflict", []

    return "promote", []
