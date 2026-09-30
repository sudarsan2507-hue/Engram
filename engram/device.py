"""A single Engram device: trusted shard (search surface) + staging shard
(pull landing zone) + SQLite quarantine store + activity log."""
import json
import os
import sqlite3
import time

from qdrant_edge import ScrollRequest

from . import shard as shard_ops
from . import crypto_utils
from .schema import Memory, TRUST_VERIFIED, SYNC
from .corroboration import resolve
from .poison_screen import screen_one


class Device:
    def __init__(self, device_id: str, data_dir: str, private_key_b64: str = None, public_key_b64: str = None):
        self.device_id = device_id
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

        if private_key_b64 is None:
            private_key_b64, public_key_b64 = crypto_utils.new_keypair()
        self.private_key = private_key_b64
        self.public_key = public_key_b64

        self.trusted = shard_ops.open_or_create(os.path.join(data_dir, "trusted"))
        self.staging = shard_ops.open_or_create(os.path.join(data_dir, "staging"))

        self.db_path = os.path.join(data_dir, "quarantine.db")
        self._init_db()

        self.offline = False
        self.log: list[dict] = []

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute(
            """CREATE TABLE IF NOT EXISTS quarantine (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, reasons TEXT NOT NULL, ts REAL NOT NULL
            )"""
        )
        con.commit()
        con.close()

    def _log(self, event: str, **data):
        entry = {"ts": time.time(), "event": event, **data}
        self.log.append(entry)
        return entry

    # ---------- local write ----------

    def write(self, text: str, subject: str, value: str, sensitivity: str = SYNC) -> Memory:
        m = Memory(text=text, subject=subject, value=value, source_device=self.device_id, sensitivity=sensitivity)
        m.sign(self.private_key)
        m.trust_status = TRUST_VERIFIED
        m.corroborated_by = [self.device_id]
        shard_ops.upsert(self.trusted, [m.to_point()])
        self._log("write", id=m.id, subject=subject, sensitivity=m.sensitivity)
        return m

    # ---------- search ----------

    def search(self, query: str, limit: int = 10):
        t0 = time.perf_counter()
        hits = shard_ops.hybrid_search(self.trusted, query, limit=limit, filter=shard_ops.trusted_filter())
        latency_ms = (time.perf_counter() - t0) * 1000
        self._log("search", query=query, hits=len(hits), latency_ms=round(latency_ms, 2))
        return hits, latency_ms

    # ---------- sync queue (what would push to hub) ----------

    def sync_queue(self) -> list[dict]:
        records, _ = self.trusted.scroll(
            ScrollRequest(filter=shard_ops.eq_filter(sensitivity=SYNC), limit=10000, with_payload=True)
        )
        return [
            {"payload": r.payload, "route_reason": "sensitivity=SYNC, trust_status=verified"}
            for r in records
            if r.payload.get("trust_status") == TRUST_VERIFIED
        ]

    def push_to_hub(self, hub_client) -> int:
        if self.offline:
            self._log("push_skipped", reason="offline")
            return 0
        items = self.sync_queue()
        if items:
            hub_client.push(self.device_id, self.public_key, [i["payload"] for i in items])
        self._log("push", count=len(items))
        return len(items)

    # ---------- pull + poison screen ----------

    def pull_from_hub(self, hub_client) -> dict:
        if self.offline:
            self._log("pull_skipped", reason="offline")
            return {"promoted": 0, "quarantined": 0, "merged": 0}

        registry = hub_client.registry()
        incoming = hub_client.pull(exclude_device=self.device_id)

        staged_points = []
        for payload in incoming:
            m = Memory.from_payload(payload)
            staged_points.append(m.to_point())
        shard_ops.upsert(self.staging, staged_points)

        promoted = quarantined = merged = 0
        for payload in incoming:
            m = Memory.from_payload(payload)
            decision, reasons = screen_one(m, registry, self.trusted)

            if decision == "quarantine":
                self._quarantine(m, reasons)
                quarantined += 1
            elif decision == "promote":
                m.trust_status = TRUST_VERIFIED
                m.corroborated_by = [m.source_device]
                shard_ops.upsert(self.trusted, [m.to_point()])
                promoted += 1
                self._log("promote", id=m.id, subject=m.subject, source=m.source_device)
            else:  # conflict -> corroboration merge
                self._merge_conflict(m, registry)
                merged += 1

        shard_ops.delete(self.staging, [p.id for p in staged_points])
        self._log("pull", promoted=promoted, quarantined=quarantined, merged=merged)
        return {"promoted": promoted, "quarantined": quarantined, "merged": merged}

    def _quarantine(self, m: Memory, reasons: list[str]):
        con = sqlite3.connect(self.db_path)
        con.execute(
            "INSERT OR REPLACE INTO quarantine (id, payload, reasons, ts) VALUES (?, ?, ?, ?)",
            (m.id, json.dumps(m.payload()), json.dumps(reasons), time.time()),
        )
        con.commit()
        con.close()
        self._log("quarantine", id=m.id, subject=m.subject, reasons=reasons)

    def quarantined(self) -> list[dict]:
        con = sqlite3.connect(self.db_path)
        rows = con.execute("SELECT id, payload, reasons, ts FROM quarantine ORDER BY ts DESC").fetchall()
        con.close()
        return [
            {"id": r[0], "payload": json.loads(r[1]), "reasons": json.loads(r[2]), "ts": r[3]}
            for r in rows
        ]

    def _merge_conflict(self, incoming: Memory, registry: dict):
        records, _ = self.trusted.scroll(
            ScrollRequest(
                filter=shard_ops.eq_filter(subject=incoming.subject, trust_status=TRUST_VERIFIED),
                limit=1000,
                with_payload=True,
            )
        )
        candidates_by_value: dict[str, list[dict]] = {}
        for r in records:
            candidates_by_value.setdefault(r.payload["value"], []).append(r.payload)
        candidates_by_value.setdefault(incoming.value, []).append(incoming.payload())

        trust_of = {d: info.get("trust_score", 0.0) for d, info in registry.items()}
        trust_of[self.device_id] = 1.0  # local device is fully trusted by itself

        result = resolve(candidates_by_value, trust_of)

        winning_payload = dict(incoming.payload())
        winning_payload.update(
            {
                "value": result["winning_value"],
                "trust_status": TRUST_VERIFIED,
                "corroborated_by": result["corroborated_by"],
                "score_breakdown": result["breakdown"],
            }
        )
        m = Memory.from_payload(winning_payload)
        shard_ops.upsert(self.trusted, [m.to_point()])
        self._log(
            "conflict_resolved",
            subject=incoming.subject,
            winning_value=result["winning_value"],
            breakdown=result["breakdown"],
        )

    # ---------- inspector data ----------

    def state(self) -> dict:
        records, _ = self.trusted.scroll(ScrollRequest(limit=10000, with_payload=True))
        return {
            "device_id": self.device_id,
            "offline": self.offline,
            "memories": [r.payload for r in records],
            "quarantine": self.quarantined(),
            "log": self.log[-200:],
        }
