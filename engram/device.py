"""One Engram device.

  trusted shard   local writes + promoted pulls; the only thing search touches,
                  always through the trust_status=verified filter
  staging shard   landing zone for pulled points; embedded once here, screened
                  here, and copied (vectors included) into trusted on promotion
  engram.db       quarantine, conflict history, pinned peer keys, sync cursors

EdgeShard snapshot restore works on whole segments, so there is no hook to
screen individual points during a snapshot sync -- hence the staging shard.
"""
import hashlib
import json
import os
import sqlite3
import threading
from collections import deque
from contextlib import contextmanager

from qdrant_edge import Point

from . import crypto_utils
from . import shard as shard_ops
from .corroboration import resolve
from .embeddings import embed_dense
from .poison_screen import screen
from .schema import (PRIVATE, SYNC, TRUST_SUPERSEDED, TRUST_VERIFIED, MalformedMemory, Memory, now_ms,
                     to_points)

PROVENANCE_FAILURES = ("unregistered_device", "registry_key_mismatch", "invalid_or_missing_signature",
                       "private_memory_on_the_wire", "malformed_payload")


class OfflineError(RuntimeError):
    pass


class Device:
    def __init__(self, device_id: str, data_dir: str):
        self.device_id = device_id
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.lock = threading.RLock()
        self.offline = False
        self.log = deque(maxlen=300)
        self.registry: dict = {}
        self._load_identity()
        self._open()

    # ---------- setup ----------

    def _load_identity(self):
        path = os.path.join(self.data_dir, "identity.json")
        if os.path.exists(path):
            with open(path) as f:
                ident = json.load(f)
        else:
            sk, pk = crypto_utils.new_keypair()
            ident = {"device_id": self.device_id, "private_key": sk, "public_key": pk}
            with open(path, "w") as f:
                json.dump(ident, f)
        self.private_key, self.public_key = ident["private_key"], ident["public_key"]

    def _open(self):
        self.trusted = shard_ops.open_or_create(os.path.join(self.data_dir, "trusted"))
        self.staging = shard_ops.open_or_create(os.path.join(self.data_dir, "staging"))
        self.db_path = os.path.join(self.data_dir, "engram.db")
        with self._db() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS quarantine (id TEXT PRIMARY KEY, payload TEXT, reasons TEXT,
                    evidence TEXT, ts INTEGER);
                CREATE TABLE IF NOT EXISTS conflicts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT,
                    winner TEXT, breakdown TEXT, trigger TEXT, ts INTEGER);
                CREATE TABLE IF NOT EXISTS pinned_keys (device_id TEXT PRIMARY KEY, public_key TEXT, ts INTEGER);
                CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
                """
            )
            con.execute("INSERT OR IGNORE INTO pinned_keys VALUES (?, ?, ?)", (self.device_id, self.public_key, now_ms()))

    @contextmanager
    def _db(self):
        # sqlite3's own context manager commits but never closes; leaked handles lock the file on Windows.
        con = sqlite3.connect(self.db_path)
        try:
            with con:
                yield con
        finally:
            con.close()

    def _kv_get(self, k, default):
        with self._db() as con:
            row = con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(row[0]) if row else default

    def _kv_set(self, k, v):
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (k, json.dumps(v)))

    def warmup(self):
        embed_dense("warmup")

    def reset(self):
        with self.lock:
            self.trusted.close()
            self.staging.close()
            for name in ("trusted", "staging"):
                shard_ops.wipe(os.path.join(self.data_dir, name))
            with self._db() as con:
                for table in ("quarantine", "conflicts", "pinned_keys", "kv"):
                    con.execute(f"DELETE FROM {table}")
            self.log.clear()
            self.registry = {}
            self.offline = False
            self._open()
            self._log("reset")

    def _log(self, event: str, **data):
        self.log.append({"ts": now_ms(), "event": event, **data})

    def pinned_keys(self) -> dict:
        with self._db() as con:
            return dict(con.execute("SELECT device_id, public_key FROM pinned_keys").fetchall())

    def _pin_new_keys(self, registry: dict):
        """Trust on first use: the first key seen for a device is pinned locally, so a hub that
        later swaps a device's key cannot make forged memories verify."""
        with self._db() as con:
            for dev, info in registry.items():
                con.execute("INSERT OR IGNORE INTO pinned_keys VALUES (?, ?, ?)", (dev, info["public_key"], now_ms()))

    # ---------- local write ----------

    def write(self, text: str, subject: str, value: str, sensitivity: str = SYNC) -> Memory:
        with self.lock:
            m = Memory(text=text, subject=subject, value=value, source_device=self.device_id,
                       sensitivity=sensitivity, corroborated_by=[self.device_id])
            m.apply_router()
            m.sign(self.private_key)
            same_subject = shard_ops.scroll_all(self.trusted, shard_ops.eq_filter(subject=subject))
            shard_ops.upsert(self.trusted, to_points([m]))
            if any(r.payload["value"] != value for r in same_subject):
                self._resolve_subject(subject, trigger=f"local write by {self.device_id}")
            elif same_subject:
                self._merge_corroboration([str(r.id) for r in same_subject], m.id, self.device_id)
            self._log("write", subject=subject, value=value, sensitivity=m.sensitivity, route=m.route_reason)
            return m

    # ---------- search ----------

    def search(self, query: str, limit: int = 10, trusted_only: bool = True):
        flt = shard_ops.trusted_filter() if trusted_only else None
        hits, timings = shard_ops.hybrid_search(self.trusted, query, limit=limit, filter=flt)
        self._log("search", query=query, hits=len(hits), trusted_only=trusted_only, **timings)
        return hits, timings

    # ---------- push ----------

    def sync_queue(self) -> list[dict]:
        """Own SYNC memories not pushed yet. Selected by filter, so PRIVATE never enters the queue."""
        flt = shard_ops.Filter(must=[
            shard_ops.match("source_device", self.device_id),
            shard_ops.match("sensitivity", SYNC),
            shard_ops.ts_after(self._kv_get("last_push_ts", 0)),
        ])
        return [r.payload for r in shard_ops.scroll_all(self.trusted, flt)]

    def held_back(self) -> list[dict]:
        return [r.payload for r in shard_ops.scroll_all(
            self.trusted, shard_ops.eq_filter(source_device=self.device_id, sensitivity=PRIVATE))]

    def push(self, hub) -> int:
        if self.offline:
            raise OfflineError("device is offline")
        with self.lock:
            queue = self.sync_queue()
            leaked = [m for m in queue if m["sensitivity"] != SYNC]
            if leaked:
                raise RuntimeError(f"router invariant violated: {len(leaked)} non-SYNC memories in push queue")
            if queue:
                hub.push(self.device_id, [self._wire(m) for m in queue])
                self._kv_set("last_push_ts", max(m["ts"] for m in queue))
            self._log("push", count=len(queue))
            return len(queue)

    @staticmethod
    def _wire(payload: dict) -> dict:
        """Only signed content + signature leave the device; local trust state never does."""
        keep = crypto_utils.CANONICAL_FIELDS + ["sensitivity", "signature"]
        return {k: payload[k] for k in keep}

    # ---------- pull + screen ----------

    def pull(self, hub) -> dict:
        if self.offline:
            raise OfflineError("device is offline")
        with self.lock:
            self.registry = hub.registry()
            self._pin_new_keys(self.registry)
            since = self._kv_get("last_pull_seq", 0)
            batch = hub.pull(since=since, exclude_device=self.device_id)
            items = batch["items"]
            summary = {"received": len(items), "skipped_known": 0, "promoted": 0, "corroborated": 0,
                       "conflicts": 0, "quarantined": 0}

            memories = []
            for raw in items:
                try:
                    m = Memory.from_payload(raw)
                except MalformedMemory as e:
                    self._quarantine(raw, ["malformed_payload"], [{"why": str(e)}])
                    summary["quarantined"] += 1
                    continue
                if self._known(m.id):
                    summary["skipped_known"] += 1
                    continue
                m.trust_status = "staged"
                m.corroborated_by = []
                m.score_breakdown = None
                m.route_reason = ""
                memories.append(m)

            if memories:
                shard_ops.upsert(self.staging, to_points(memories))
                staged = {str(r.id): r for r in self.staging.retrieve([m.id for m in memories], True, True)}
                for m in memories:
                    rec = staged[m.id]
                    verdict = screen(m, rec.vector["dense"], self.registry, self.pinned_keys(), self.trusted)
                    self._apply(verdict, m, rec)
                    summary[{"promote": "promoted", "corroborate": "corroborated", "conflict": "conflicts",
                             "quarantine": "quarantined"}[verdict.decision]] += 1
                shard_ops.delete(self.staging, [m.id for m in memories])

            self._kv_set("last_pull_seq", batch["next_since"])
            flow = self._kv_get("flow", {})
            self._kv_set("flow", {k: flow.get(k, 0) + v for k, v in summary.items()})
            self._log("pull", since=since, **summary)
            return summary

    def _known(self, mem_id: str) -> bool:
        if self.trusted.retrieve([mem_id], False, False):
            return True
        with self._db() as con:
            return con.execute("SELECT 1 FROM quarantine WHERE id=?", (mem_id,)).fetchone() is not None

    def _promote(self, m: Memory, rec, **payload_overrides) -> None:
        payload = {**m.payload(), "trust_status": TRUST_VERIFIED, "corroborated_by": [m.source_device],
                   **payload_overrides}
        shard_ops.upsert(self.trusted, [Point(id=m.id, vector=rec.vector, payload=payload)])

    def _apply(self, verdict, m: Memory, rec):
        if verdict.decision == "quarantine":
            self._quarantine(m.payload(), verdict.reasons, verdict.evidence)
        elif verdict.decision == "promote":
            self._promote(m, rec)
            self._log("promote", subject=m.subject, value=m.value, source=m.source_device)
        elif verdict.decision == "corroborate":
            self._promote(m, rec)
            self._merge_corroboration(verdict.same_value_ids, m.id, m.source_device)
        else:
            self._promote(m, rec)
            self._resolve_subject(m.subject, trigger=f"pull from {m.source_device}")

    def _merge_corroboration(self, existing_ids: list[str], new_id: str, source: str):
        ids = existing_ids + [new_id]
        recs = self.trusted.retrieve(ids, True, False)
        devices = sorted({d for r in recs for d in r.payload.get("corroborated_by", [])} | {source})
        shard_ops.set_payload(self.trusted, ids, {"corroborated_by": devices})
        subject = recs[0].payload["subject"]
        self._log("corroborate", subject=subject, value=recs[0].payload["value"], corroborated_by=devices)

    def _resolve_subject(self, subject: str, trigger: str):
        """Re-score every candidate (verified or previously superseded) for a subject. Signed content
        is never rewritten: winners are marked verified, losers superseded and filtered out of search."""
        recs = shard_ops.scroll_all(self.trusted, shard_ops.eq_filter(
            subject=subject, trust_status=[TRUST_VERIFIED, TRUST_SUPERSEDED]))
        by_value: dict[str, list] = {}
        for r in recs:
            by_value.setdefault(r.payload["value"], []).append(r)
        trust_of = {d: info.get("trust_score", 0.0) for d, info in self.registry.items()}
        result = resolve({v: [r.payload for r in rs] for v, rs in by_value.items()}, trust_of)
        for value, rs in by_value.items():
            status = TRUST_VERIFIED if value == result["winning_value"] else TRUST_SUPERSEDED
            shard_ops.set_payload(self.trusted, [str(r.id) for r in rs], {
                "trust_status": status,
                "corroborated_by": result["breakdown"][value]["devices"],
                "score_breakdown": result["breakdown"],
            })
        with self._db() as con:
            con.execute("INSERT INTO conflicts (subject, winner, breakdown, trigger, ts) VALUES (?, ?, ?, ?, ?)",
                        (subject, result["winning_value"], json.dumps(result["breakdown"]), trigger, now_ms()))
        self._log("conflict_resolved", subject=subject, winner=result["winning_value"],
                  scores={v: b["score"] for v, b in result["breakdown"].items()})

    # ---------- quarantine ----------

    def _quarantine(self, payload, reasons: list[str], evidence: list[dict]):
        qid = payload.get("id") if isinstance(payload, dict) and isinstance(payload.get("id"), str) else None
        qid = qid or hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:32]
        with self._db() as con:
            con.execute("INSERT OR REPLACE INTO quarantine VALUES (?, ?, ?, ?, ?)",
                        (qid, json.dumps(payload, default=str), json.dumps(reasons), json.dumps(evidence), now_ms()))
        subject = payload.get("subject") if isinstance(payload, dict) else None
        self._log("quarantine", subject=subject, reasons=reasons)

    def quarantined(self) -> list[dict]:
        with self._db() as con:
            rows = con.execute("SELECT id, payload, reasons, evidence, ts FROM quarantine ORDER BY ts DESC").fetchall()
        return [{"id": r[0], "payload": json.loads(r[1]), "reasons": json.loads(r[2]),
                 "evidence": json.loads(r[3]), "ts": r[4]} for r in rows]

    def release(self, qid: str) -> dict:
        """Operator override for semantic/trust quarantines. Provenance failures are never releasable:
        a memory whose author cannot be proven stays out."""
        with self.lock:
            with self._db() as con:
                row = con.execute("SELECT payload, reasons FROM quarantine WHERE id=?", (qid,)).fetchone()
            if row is None:
                return {"ok": False, "reason": "not found"}
            payload, reasons = json.loads(row[0]), json.loads(row[1])
            blocking = [r for r in reasons if r in PROVENANCE_FAILURES]
            if blocking:
                return {"ok": False, "reason": f"provenance failure is not releasable: {blocking}"}
            m = Memory.from_payload(payload)
            shard_ops.upsert(self.trusted, to_points([m]))
            shard_ops.set_payload(self.trusted, [m.id], {"trust_status": TRUST_VERIFIED,
                                                          "corroborated_by": [m.source_device]})
            with self._db() as con:
                con.execute("DELETE FROM quarantine WHERE id=?", (qid,))
            self._resolve_subject(m.subject, trigger=f"operator released {m.source_device}'s memory")
            self._log("release", subject=m.subject, value=m.value)
            return {"ok": True}

    # ---------- inspector ----------

    def conflicts(self) -> list[dict]:
        with self._db() as con:
            rows = con.execute("SELECT subject, winner, breakdown, trigger, ts FROM conflicts ORDER BY id DESC").fetchall()
        return [{"subject": r[0], "winner": r[1], "breakdown": json.loads(r[2]), "trigger": r[3], "ts": r[4]}
                for r in rows]

    def stats(self) -> dict:
        c = lambda **kw: shard_ops.count(self.trusted, shard_ops.eq_filter(**kw) if kw else None)
        return {
            "total": c(),
            "verified": c(trust_status=TRUST_VERIFIED),
            "superseded": c(trust_status=TRUST_SUPERSEDED),
            "private": c(sensitivity=PRIVATE),
            "from_peers": shard_ops.count(self.trusted, shard_ops.Filter(
                must_not=[shard_ops.match("source_device", self.device_id)])),
        }

    def state(self) -> dict:
        with self.lock:
            pinned = self.pinned_keys()
            memories = []
            for r in shard_ops.scroll_all(self.trusted):
                p = dict(r.payload)
                p["sig_valid"] = crypto_utils.verify(p, p.get("signature", ""), pinned.get(p["source_device"], ""))
                memories.append(p)
            memories.sort(key=lambda p: (p["subject"], p["ts"]))
            return {
                "device_id": self.device_id,
                "public_key": self.public_key,
                "offline": self.offline,
                "stats": self.stats(),
                "flow": self._kv_get("flow", {}),
                "memories": memories,
                "sync_queue": self.sync_queue(),
                "held_back": self.held_back(),
                "quarantine": self.quarantined(),
                "conflicts": self.conflicts(),
                "pinned_keys": pinned,
                "registry": self.registry,
                "log": list(self.log)[::-1][:80],
            }
