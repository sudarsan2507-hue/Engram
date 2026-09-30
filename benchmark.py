"""Bulk-seed synthetic memories and measure filtered vs unfiltered hybrid
search latency (p50/p95) plus process RSS. Numbers are measured, never
hardcoded -- run this to regenerate them."""
import argparse
import os
import random
import shutil
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))

import psutil

from engram import shard as shard_ops
from engram.schema import Memory, TRUST_VERIFIED
from engram import crypto_utils

SUBJECTS = ["battery", "dock1", "dock2", "dock3", "route_a", "route_b", "sensor_temp", "sensor_humidity", "wifi", "firmware"]
VALUES = ["ok", "broken", "80%", "20%", "offline", "online", "calibrated", "drifted"]
WORDS = ["robot", "kiosk", "status", "report", "reading", "check", "update", "log", "event", "alert",
         "charger", "sensor", "battery", "route", "dock", "warning", "nominal", "degraded", "sync", "scan"]


def synth_text(i: int) -> str:
    return " ".join(random.choice(WORDS) for _ in range(random.randint(6, 14))) + f" #{i}"


def percentile(values, p):
    values = sorted(values)
    k = int(round((p / 100) * (len(values) - 1)))
    return values[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--queries", type=int, default=100)
    ap.add_argument("--data-dir", default=os.path.join(os.path.dirname(__file__), "data", "benchmark"))
    args = ap.parse_args()

    if os.path.isdir(args.data_dir):
        shutil.rmtree(args.data_dir)

    proc = psutil.Process()
    rss_start_mb = proc.memory_info().rss / (1024 * 1024)

    shard = shard_ops.open_or_create(args.data_dir)
    sk, pk = crypto_utils.new_keypair()

    print(f"Seeding {args.n} synthetic memories...")
    t_seed0 = time.perf_counter()
    batch = []
    for i in range(args.n):
        subject = random.choice(SUBJECTS)
        value = random.choice(VALUES)
        m = Memory(text=synth_text(i), subject=subject, value=value, source_device="bench")
        m.sign(sk)
        m.trust_status = TRUST_VERIFIED if random.random() > 0.05 else "quarantined"
        batch.append(m.to_point())
        if len(batch) >= 500:
            shard_ops.upsert(shard, batch)
            batch = []
        if i % 5000 == 0 and i > 0:
            print(f"  ...{i}")
    shard_ops.upsert(shard, batch)
    shard.flush()
    t_seed = time.perf_counter() - t_seed0
    print(f"Seed done in {t_seed:.1f}s")

    rss_after_seed_mb = proc.memory_info().rss / (1024 * 1024)

    queries = [synth_text(1_000_000 + i) for i in range(args.queries)]

    def run(filter_):
        latencies = []
        for q in queries:
            t0 = time.perf_counter()
            shard_ops.hybrid_search(shard, q, limit=10, filter=filter_)
            latencies.append((time.perf_counter() - t0) * 1000)
        return latencies

    print("Benchmarking WITHOUT filter...")
    lat_no_filter = run(None)
    print("Benchmarking WITH filter (trust_status=verified)...")
    lat_filtered = run(shard_ops.trusted_filter())

    rss_end_mb = proc.memory_info().rss / (1024 * 1024)

    print("\n=== Results ===")
    print(f"Seeded points:         {args.n}")
    print(f"Seed time:             {t_seed:.1f}s ({args.n / t_seed:.0f} pts/s)")
    print(f"RSS before seed:       {rss_start_mb:.1f} MB")
    print(f"RSS after seed:        {rss_after_seed_mb:.1f} MB")
    print(f"RSS after benchmark:   {rss_end_mb:.1f} MB")
    print(f"\nUnfiltered search  p50={percentile(lat_no_filter, 50):.2f}ms  p95={percentile(lat_no_filter, 95):.2f}ms")
    print(f"Filtered search    p50={percentile(lat_filtered, 50):.2f}ms  p95={percentile(lat_filtered, 95):.2f}ms")


if __name__ == "__main__":
    main()
