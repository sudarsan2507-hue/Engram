# Engram — tamper-proof edge memory (Qdrant Edge hackathon)

Offline-first AI memory for edge devices, built on `qdrant-edge-py`. The
differentiator: the sync layer decides what to **trust**. Memory poisoning is
treated as an attack, not a data-quality nuisance.

## Architecture

Each simulated device (`robot`, `kiosk`) is its own process with its own data
directory and two local `EdgeShard`s:

- **trusted shard** — local writes + promoted pulls. This is the only thing
  every search queries, via a payload filter (`trust_status=verified`).
- **staging shard** — landing zone for points pulled from the hub. Points are
  screened here before promotion, never searched directly.

This exists because `EdgeShard.update_from_snapshot()` / `snapshot_manifest()`
restore/describe whole segments, not individual points — there is no API hook
to screen a point mid-restore. So sync goes through a staging shard instead of
a snapshot restore straight into the trusted shard.

The **hub** (`engram/hub.py`) is a small FastAPI service holding a device
registry (`device_id -> public_key, trust_score`) and a dumb REST store for
pushed memories. It does **no** screening — it is intentionally untrusted.
Every device screens on pull, not the hub, and not the pushing device.

```
robot                              hub (FastAPI, :8000)              kiosk
 trusted shard <--search             registry: {device_id: pubkey,     trusted shard <--search
 staging shard <--pull-----+         trust_score}                     staging shard <--pull------+
      |                    |         store: {mem_id: payload}              |                     |
      +--poison screen-----+                  ^    |                       +--poison screen------+
                                    push       |    | pull
                                    (SYNC only)+----+
```

## Trust boundary = payload filter

Every search on the trusted shard runs with a required filter:
`trust_status = "verified"`. Payload indexes (`trust_status`, `sensitivity`,
`subject`, `source_device`) are created on shard init so these filters hit the
index rather than a full scan — see `engram/shard.py::INDEXED_FIELDS`.

## Poison screen (runs on every pull, before promotion)

For each point landed in staging (`engram/poison_screen.py::screen_one`):

1. `sensitivity == PRIVATE` → quarantine (defense in depth; the router should
   never have queued it, screened again anyway).
2. Source device not in the registry → quarantine (`unregistered_device`).
3. Ed25519 signature doesn't verify against the registered public key →
   quarantine (`invalid_or_missing_signature`).
4. `trust_score < 0.4` → quarantine (`low_trust_source`).
5. Contradicts a **corroborated** local memory (same subject, different
   value, existing memory already has ≥2 agreeing devices) → quarantine
   (`contradicts_corroborated_memory`).
6. Contradicts a *non-corroborated* local memory → genuine conflict, routed to
   corroboration merge, not silently overwritten.
7. Otherwise → promote to the trusted shard as `verified`.

## Corroboration merge (`engram/corroboration.py`)

For a subject with competing values, each value's supporting memories score:

```
score = avg(trust_score of agreeing devices) * recency_decay(latest_ts) * distinct_agreeing_devices
```

`recency_decay` halves every 7 days. The highest-scoring value wins; the full
per-value breakdown is stored on the winning memory's `score_breakdown` field
and shown in the log / inspector — never silently computed and discarded.

## Provenance

Every memory is Ed25519-signed over its canonical content fields (`id, text,
subject, value, source_device, ts` — sorted-key JSON). Editing any of those
fields invalidates the signature; `Memory.verify()` checks against the
signer's registered public key. (The original hash-chain design was cut under
the deadline — see Cuts below. Signatures alone are enough to catch tampering
and forged authorship, which is what the poison screen needs.)

## Router

`Memory.__post_init__` regex-scans `text`/`value` for email addresses and a
couple of other PII-shaped patterns and forces `sensitivity = PRIVATE`.
`Device.sync_queue()` only selects `sensitivity = SYNC` memories, and
`push_to_hub` only ever sends that queue — PRIVATE memories never reach the
hub, enforced at both the query and the push boundary.

## Running it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # .venv/bin/pip on macOS/Linux
./run_all.sh     # wipes ./data, starts hub :8000, robot :8001, kiosk :8002
python demo.py   # scripted seed -> offline search -> sync -> attack -> quarantine -> conflict merge
```

Open `http://127.0.0.1:8001/` or `http://127.0.0.1:8002/` — either serves the
same two-panel inspector (robot + kiosk side by side, cross-origin fetches via
CORS). Search box, sync button, offline toggle, and an "inject attack" button
that POSTs a deliberately unsigned memory straight into the hub.

`./stop_all.sh` stops all three processes.

## Benchmark

```bash
.venv/Scripts/python.exe benchmark.py --n 50000 --queries 100
```

Bulk-seeds 50k synthetic memories into a shard and reports p50/p95 hybrid
search latency with vs without the `trust_status` filter, plus process RSS.
Numbers are measured by this script, not hardcoded — rerun it to regenerate
them; see `benchmark_output.log` for the last run on this machine.

## Tests

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
```

8 tests, all against the real `qdrant_edge` API and real Ed25519 signing (no
mocks): sign/verify roundtrip, tamper detection, PII routing, filtered hybrid
search on a real shard, poison screen rejecting unregistered devices and bad
signatures, poison screen promoting a clean new memory, and corroboration
picking the value with more agreeing devices.

## Honest limits (MOCK / cut for the deadline)

- **Mock**: `engram/seed_data/{robot,kiosk}.json` are hand-written demo
  memories, not from a real sensor or LLM. Labeled MOCK in this README and in
  the UI banner.
- **Cut**: hash-chain provenance (signatures alone are used — see Provenance
  above).
- **Cut**: decay/consolidation of old memories — not implemented.
- **Cut**: partial-snapshot sync. Sync is plain REST push/pull through the
  hub's in-memory store, not `EdgeShard` snapshot transfer. This was a
  deliberate simplification, not an oversight — see the Architecture section
  for why point-level snapshot restore isn't available in the API anyway.
- **Static config**: trust scores are set once at hub registration
  (`trust_score=0.8` for both devices in this demo) and never adapt based on
  behavior.
- **Simulated network**: "offline" is a boolean flag on each device that
  short-circuits hub calls, not a real network partition.
- **No React**: the inspector is a single static HTML page served by FastAPI.
- All code here is original, written against the `qdrant_edge` API as
  reference (see `introspect_edge.py` for how the real API was confirmed
  before building on top of it) — no code was copied from other Edge track
  repos.
