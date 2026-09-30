"""Conflict resolution for competing values on the same subject.

    score(value) = avg_trust(agreeing devices) * recency_decay(newest ts) * distinct_agreeing_devices

Every device scores with the same registry trust (no self-bias) and the same
deterministic tie-break, so two devices holding the same candidates pick the
same winner. The ranking does not depend on *when* it is computed: decay is
exponential, so the ratio between two candidates' decays depends only on the
gap between their timestamps.
"""
import time

RECENCY_HALFLIFE_MS = 7 * 24 * 3600 * 1000


def recency_decay(ts_ms: int, now_ms: int) -> float:
    return 0.5 ** (max(0, now_ms - ts_ms) / RECENCY_HALFLIFE_MS)


def score_candidate(memories: list[dict], trust_of: dict, now_ms: int) -> dict:
    devices = sorted({m["source_device"] for m in memories})
    avg_trust = sum(trust_of.get(d, 0.0) for d in devices) / len(devices)
    newest = max(m["ts"] for m in memories)
    decay = recency_decay(newest, now_ms)
    return {
        "devices": devices,
        "avg_trust": round(avg_trust, 4),
        "recency_decay": round(decay, 4),
        "distinct_agreeing_devices": len(devices),
        "newest_ts": newest,
        "score": round(avg_trust * decay * len(devices), 4),
        "formula": f"{avg_trust:.2f} x {decay:.4f} x {len(devices)}",
    }


def resolve(candidates_by_value: dict[str, list[dict]], trust_of: dict, now_ms: int = None) -> dict:
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    breakdown = {v: score_candidate(ms, trust_of, now_ms) for v, ms in candidates_by_value.items()}
    raw = {v: b["avg_trust"] * b["recency_decay"] * b["distinct_agreeing_devices"] for v, b in breakdown.items()}
    winner = max(breakdown, key=lambda v: (raw[v], breakdown[v]["newest_ts"], v))
    return {
        "winning_value": winner,
        "breakdown": breakdown,
        "corroborated_by": breakdown[winner]["devices"],
    }
