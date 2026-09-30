"""Conflict resolution for competing values on the same subject.

score = trust * recency_decay * distinct_agreeing_devices
recency_decay halves every RECENCY_HALFLIFE_SECONDS.
"""
import math
import time

RECENCY_HALFLIFE_SECONDS = 7 * 24 * 3600  # 1 week


def recency_decay(ts: float, now: float = None) -> float:
    now = now or time.time()
    age = max(0.0, now - ts)
    return 0.5 ** (age / RECENCY_HALFLIFE_SECONDS)


def score_candidate(memories: list[dict], trust_of: dict, now: float = None) -> dict:
    """memories: all memories (local + incoming) agreeing on one value for a subject."""
    now = now or time.time()
    devices = {m["source_device"] for m in memories}
    avg_trust = sum(trust_of.get(d, 0.0) for d in devices) / len(devices)
    latest_ts = max(m["ts"] for m in memories)
    decay = recency_decay(latest_ts, now)
    n_devices = len(devices)
    score = avg_trust * decay * n_devices
    return {
        "devices": sorted(devices),
        "avg_trust": round(avg_trust, 4),
        "recency_decay": round(decay, 4),
        "distinct_agreeing_devices": n_devices,
        "score": round(score, 4),
    }


def resolve(candidates_by_value: dict[str, list[dict]], trust_of: dict, now: float = None) -> dict:
    """Returns {winning_value, breakdown: {value: score_info}, corroborated_by: [devices]}."""
    breakdown = {
        value: score_candidate(mems, trust_of, now)
        for value, mems in candidates_by_value.items()
    }
    winning_value = max(breakdown, key=lambda v: breakdown[v]["score"])
    return {
        "winning_value": winning_value,
        "breakdown": breakdown,
        "corroborated_by": breakdown[winning_value]["devices"],
    }
