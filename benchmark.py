"""Edge benchmark: filtered hybrid search on a realistic synthetic memory store.

Everything reported is measured by this script on this machine. Results go to
stdout and bench_results.json (shown in the inspector).

  - documents are embedded once, in batches; embedding throughput is reported
  - the shard is optimized (HNSW built) before any query is timed
  - query embedding is timed separately from vector search
  - filters are swept across selectivities, with and without payload indexes,
    and with ACORN; dense recall@10 is checked against exact search
  - an int8 scalar-quantized copy (same vectors) is compared for disk/latency/recall
"""
import argparse
import json
import os
import platform
import random
import shutil
import statistics
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import psutil
from qdrant_edge import (AcornSearchParams, Filter, Point, Query, QueryRequest, ScalarQuantizationConfig,
                         ScalarType, SearchParams, SparseVector)

from engram import shard as shard_ops
from engram.embeddings import document_vectors, embed_dense, embed_sparse_query

ROOT = os.path.dirname(os.path.abspath(__file__))
DEVICES = [f"device-{i:02d}" for i in range(50)]
SUBJECTS = ["dock1", "dock2", "dock3", "battery", "route_a", "route_b", "lidar", "wifi", "firmware", "motor_temp"]
PLACES = ["zone A", "zone B", "loading bay", "entrance", "aisle 4", "cold store", "charging row", "mezzanine"]
STATES = ["nominal", "degraded", "offline", "recalibrated", "overheating", "fault cleared", "sparking", "intermittent"]
VERBS = ["reported", "observed", "confirmed", "logged", "flagged", "measured"]
DAY_MS = 86_400_000


def synth(i: int, rng: random.Random, now: int) -> tuple[str, dict]:
    subject = rng.choice(SUBJECTS)
    text = (f"{rng.choice(VERBS)} {subject.replace('_', ' ')} {rng.choice(STATES)} near {rng.choice(PLACES)}, "
            f"reading {rng.randint(1, 999)} at shift {rng.randint(1, 3)}")
    payload = {
        "id": str(uuid.UUID(int=rng.getrandbits(128))),
        "text": text, "subject": subject, "value": rng.choice(STATES),
        "source_device": rng.choice(DEVICES),
        "ts": now - rng.randint(0, 30 * DAY_MS),
        "sensitivity": "PRIVATE" if rng.random() < 0.2 else "SYNC",
        "trust_status": "verified" if rng.random() < 0.9 else "superseded",
    }
    return text, payload


def pct(values, p):
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))], 3)


def rss_mb():
    return round(psutil.Process().memory_info().rss / 2**20, 1)


def dir_mb(path):
    return round(sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(path) for f in fs) / 2**20, 1)


def timed(fn, runs):
    lat = []
    for args in runs:
        t = time.perf_counter()
        fn(*args)
        lat.append((time.perf_counter() - t) * 1000)
    return {"p50_ms": pct(lat, 50), "p95_ms": pct(lat, 95)}


def dense_ids(shard, dense, flt, params):
    hits = shard.query(QueryRequest(limit=10, query=Query.Nearest(dense, using="dense"), filter=flt, params=params))
    return [str(h.id) for h in hits]


def recall(shard, queries, filters, params):
    scores = []
    for (dense, _), flt in zip(queries, filters):
        truth = set(dense_ids(shard, dense, flt, SearchParams(exact=True)))
        if truth:
            scores.append(len(truth & set(dense_ids(shard, dense, flt, params))) / len(truth))
    return round(statistics.mean(scores), 4) if scores else None


def load_or_embed(texts: list[str], root: str) -> tuple[list[dict], float]:
    """Embedding 50k docs on a laptop CPU takes ~10 min; cache vectors so reruns only re-time search.
    The reported embed time is always the one measured when the cache was built."""
    path = os.path.join(root, f"embeddings_{len(texts)}.npz")
    if os.path.exists(path):
        z = np.load(path)
        dense, idx, val, off = z["dense"], z["idx"], z["val"], z["off"]
        vectors = [{"dense": dense[i].tolist(),
                    "bm25": SparseVector(indices=idx[off[i]:off[i + 1]].tolist(), values=val[off[i]:off[i + 1]].tolist())}
                   for i in range(len(texts))]
        print(f"loaded cached embeddings ({path}); embed time from cache build: {float(z['embed_s']):.1f}s")
        return vectors, float(z["embed_s"])
    print(f"embedding {len(texts):,} memories (batched)...")
    t = time.perf_counter()
    vectors = document_vectors(texts)
    embed_s = time.perf_counter() - t
    print(f"  embedded in {embed_s:.1f}s ({len(texts) / embed_s:.0f} docs/s)")
    os.makedirs(root, exist_ok=True)
    lens = [len(v["bm25"].indices) for v in vectors]
    np.savez(path, dense=np.array([v["dense"] for v in vectors], dtype=np.float32),
             idx=np.concatenate([v["bm25"].indices for v in vectors]).astype(np.uint32),
             val=np.concatenate([v["bm25"].values for v in vectors]).astype(np.float32),
             off=np.concatenate([[0], np.cumsum(lens)]).astype(np.int64), embed_s=embed_s)
    return vectors, embed_s


def build(path, points, quantization=None, batch=1000):
    shutil.rmtree(path, ignore_errors=True)
    shard = shard_ops.open_or_create(path, quantization=quantization)
    t = time.perf_counter()
    for i in range(0, len(points), batch):
        shard_ops.upsert(shard, points[i:i + batch])
    shard.flush()
    upsert_s = time.perf_counter() - t
    t = time.perf_counter()
    shard.optimize()
    return shard, round(upsert_s, 1), round(time.perf_counter() - t, 1)


def measure_device_footprint(shard_dir: str) -> dict:
    """Runs in a fresh process: what a device holding this shard costs in RAM (model + shard, nothing else)."""
    rss = {"start": rss_mb()}
    embed_dense("warmup")
    rss["model_loaded"] = rss_mb()
    t = time.perf_counter()
    shard = shard_ops.open_or_create(shard_dir)
    load_s = time.perf_counter() - t
    t = time.perf_counter()
    shard_ops.create_indexes(shard)
    index_s = time.perf_counter() - t
    rss["shard_loaded"] = rss_mb()
    for i in range(100):
        dense, sparse = shard_ops.query_vectors(f"dock {i % 10} charger sparking near zone {i % 8}")
        shard_ops.hybrid_query(shard, dense, sparse, filter=shard_ops.trusted_filter())
    rss["after_100_queries"] = rss_mb()
    return {"rss_mb": rss, "shard_load_s": round(load_s, 2), "index_build_s": round(index_s, 2)}


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--measure-footprint":
        print(json.dumps(measure_device_footprint(sys.argv[2])))
        return
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--queries", type=int, default=200)
    ap.add_argument("--dir", default=os.path.join(ROOT, "bench_data"))
    ap.add_argument("--no-int8", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    rng = random.Random(7)
    now = int(time.time() * 1000)
    rss = {"start": rss_mb()}
    embed_dense("warmup")
    rss["model_loaded"] = rss_mb()

    docs = [synth(i, rng, now) for i in range(args.n)]
    vectors, embed_s = load_or_embed([text for text, _ in docs], args.dir)
    points = [Point(id=p["id"], vector=v, payload=p) for (_, p), v in zip(docs, vectors)]

    shard, upsert_s, optimize_s = build(os.path.join(args.dir, "float32"), points)
    rss["after_seed"] = rss_mb()
    print(f"  upsert {upsert_s}s, optimize/HNSW {optimize_s}s, segments={shard.info().segments_count}")

    qrng = random.Random(11)
    qtexts = [synth(0, qrng, now)[0] for _ in range(args.queries)]
    t_embed = []
    queries = []
    for q in qtexts:
        t = time.perf_counter()
        queries.append((embed_dense(q), embed_sparse_query(q)))
        t_embed.append((time.perf_counter() - t) * 1000)

    verified = shard_ops.match("trust_status", "verified")
    scenario_filters = {
        "no filter": lambda i: None,
        "trust_status=verified": lambda i: Filter(must=[verified]),
        "verified AND sensitivity=SYNC": lambda i: Filter(must=[verified, shard_ops.match("sensitivity", "SYNC")]),
        "verified AND subject": lambda i: Filter(must=[verified, shard_ops.match("subject", SUBJECTS[i % 10])]),
        "verified AND source_device": lambda i: Filter(must=[verified, shard_ops.match("source_device", DEVICES[i % 50])]),
        "verified AND subject AND device": lambda i: Filter(must=[verified, shard_ops.match("subject", SUBJECTS[i % 10]),
                                                                  shard_ops.match("source_device", DEVICES[i % 50])]),
        "verified AND last 24h (range)": lambda i: Filter(must=[verified, shard_ops.ts_after(now - DAY_MS)]),
    }

    def run_hybrid(flts, params=None, scoped_idf=True):
        return timed(lambda d, s, f: shard_ops.hybrid_query(shard, d, s, filter=f, params=params, scoped_idf=scoped_idf),
                     [(d, s, f) for (d, s), f in zip(queries, flts)])

    scenarios = []
    print("\nvector search latency, hybrid dense+BM25+RRF, query embedding excluded")
    print(f"  {'filter':<34} {'passes':>7}   {'filter only (global IDF)':>26}   {'+ trust-scoped BM25 IDF':>26}")
    for name, mk in scenario_filters.items():
        flts = [mk(i) for i in range(args.queries)]
        sel = None if flts[0] is None else round(statistics.mean(
            shard_ops.count(shard, f) for f in flts[:20]) / args.n, 5)
        plain = run_hybrid(flts, scoped_idf=False)
        scoped = run_hybrid(flts) if flts[0] is not None else plain
        scenarios.append({"name": name, "selectivity": sel, **plain,
                          "scoped_p50_ms": scoped["p50_ms"], "scoped_p95_ms": scoped["p95_ms"]})
        print(f"  {name:<34} {'-' if sel is None else f'{sel:.2%}':>7}   p50 {plain['p50_ms']:>6} p95 {plain['p95_ms']:>6} ms"
              f"   p50 {scoped['p50_ms']:>6} p95 {scoped['p95_ms']:>6} ms")

    trusted = [Filter(must=[verified])] * args.queries
    selective = [scenario_filters["verified AND subject AND device"](i) for i in range(args.queries)]
    acorn = SearchParams(acorn=AcornSearchParams(enable=True))
    r = run_hybrid(selective, params=acorn, scoped_idf=False)
    scenarios.append({"name": "verified AND subject AND device + ACORN", "selectivity": None, **r})
    print(f"  {'selective filter + ACORN':<34} {'':>7}   p50 {r['p50_ms']:>6} p95 {r['p95_ms']:>6} ms")

    mid = [scenario_filters["verified AND source_device"](i) for i in range(args.queries)]
    rec = {
        "trusted filter": recall(shard, queries, trusted, None),
        "source_device filter": recall(shard, queries, mid, None),
        "source_device filter + ACORN": recall(shard, queries, mid, acorn),
        "subject+device filter": recall(shard, queries, selective, None),
    }
    print("\ndense recall@10 vs exact search:", rec)

    shard_ops.drop_indexes(shard)
    for name in ("verified AND source_device", "verified AND subject AND device"):
        flts = [scenario_filters[name](i) for i in range(min(50, args.queries))]  # ~0.5 s each unindexed
        r = run_hybrid(flts, scoped_idf=False)
        scenarios.append({"name": f"{name} (NO payload index)", "selectivity": None, **r,
                          "note": f"same query, indexes dropped, {len(flts)} queries"})
        print(f"  {name + ' (NO index)':<34} {'':>7}   p50 {r['p50_ms']:>6} p95 {r['p95_ms']:>6} ms")
    rss["after_bench"] = rss_mb()
    disk = {"float32": dir_mb(os.path.join(args.dir, "float32"))}
    shard.close()

    int8 = None
    if not args.no_int8:
        print("\nint8 scalar-quantized copy (same vectors)...")
        q = ScalarQuantizationConfig(type=ScalarType.Int8, always_ram=True)
        qshard, q_upsert, q_opt = build(os.path.join(args.dir, "int8"), points, quantization=q)
        lat = timed(lambda d, s, f: shard_ops.hybrid_query(qshard, d, s, filter=f, scoped_idf=False),
                    [(d, s, f) for (d, s), f in zip(queries, trusted)])
        int8 = {"optimize_s": q_opt, **lat, "recall_trusted": recall(qshard, queries, trusted, None)}
        disk["int8"] = dir_mb(os.path.join(args.dir, "int8"))
        print(f"  trusted filter p50={lat['p50_ms']}ms p95={lat['p95_ms']}ms recall@10={int8['recall_trusted']} "
              f"disk={disk['int8']}MB")
        qshard.close()

    print("\nfresh-process device footprint (model + 50k shard, no benchmark data in memory)...")
    out = subprocess.run([sys.executable, os.path.abspath(__file__), "--measure-footprint",
                          os.path.join(args.dir, "float32")], capture_output=True, text=True, cwd=ROOT)
    footprint = json.loads(out.stdout.strip().splitlines()[-1])
    print(f"  {footprint}")

    results = {
        "n_points": args.n,
        "device_footprint": footprint,
        "queries": args.queries,
        "machine": f"{platform.system()} {platform.release()}, {platform.processor() or platform.machine()}, "
                   f"{psutil.cpu_count(logical=False)} cores, Python {platform.python_version()}",
        "seed": {"embed_s": round(embed_s, 1), "upsert_s": upsert_s, "optimize_s": optimize_s,
                 "total_s": round(embed_s + upsert_s + optimize_s, 1),
                 "points_per_s": round(args.n / (embed_s + upsert_s), 0)},
        "query_embed_ms": {"p50": pct(t_embed, 50), "p95": pct(t_embed, 95)},
        "scenarios": scenarios,
        "recall_at_10": rec,
        "int8": int8,
        "rss_mb": rss,
        "disk_mb": disk,
    }
    with open(os.path.join(ROOT, "bench_results.json"), "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nquery embedding p50={results['query_embed_ms']['p50']}ms  RSS={rss}  disk={disk}")
    print("wrote bench_results.json")


if __name__ == "__main__":
    main()
