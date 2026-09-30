"""Unit tests against the real qdrant_edge API and real Ed25519 -- no mocks."""
import uuid

from qdrant_edge import Point

from engram import crypto_utils
from engram import shard as shard_ops
from engram.corroboration import resolve
from engram.schema import PRIVATE, SYNC, MalformedMemory, Memory, to_points

import pytest


def signed(text="the dock 3 charger is sparking", subject="dock3", value="broken", source="robot"):
    sk, pk = crypto_utils.new_keypair()
    m = Memory(text=text, subject=subject, value=value, source_device=source)
    m.sign(sk)
    return m, pk


def test_sign_verify_roundtrip():
    m, pk = signed()
    assert m.verify(pk)


def test_tampered_value_fails():
    m, pk = signed()
    m.value = "safe to ignore"
    assert not m.verify(pk)


def test_signature_survives_shard_roundtrip(tmp_path):
    """Regression: float timestamps drifted 1 ULP through the payload store and broke signatures."""
    s = shard_ops.open_or_create(str(tmp_path))
    pairs = [signed(text=f"reading number {i}") for i in range(40)]
    shard_ops.upsert(s, to_points([m for m, _ in pairs]))
    for m, pk in pairs:
        stored = s.retrieve([m.id], True, False)[0].payload
        assert crypto_utils.verify(stored, stored["signature"], pk)


@pytest.mark.parametrize("text,kind", [
    ("contact ops-lead@warehouse.example", "email"),
    ("ssn 123-45-6789 on file", "ssn"),
    ("the wifi password is hunter2", "credential"),
])
def test_router_forces_private(text, kind):
    m = Memory(text=text, subject="x", value="y", source_device="robot", sensitivity=SYNC)
    m.apply_router()
    assert m.sensitivity == PRIVATE and kind in m.route_reason


def test_router_keeps_plain_text_sync():
    m = Memory(text="battery at 80 percent", subject="battery", value="80%", source_device="robot")
    m.apply_router()
    assert m.sensitivity == SYNC


@pytest.mark.parametrize("payload", [
    {"id": "not-a-uuid", "text": "a", "subject": "b", "value": "c", "source_device": "d", "ts": 1},
    {"id": str(uuid.uuid4()), "text": "a", "subject": "b", "value": "c", "source_device": "d", "ts": 1.5},
    {"id": str(uuid.uuid4()), "text": "a"},
    ["not", "an", "object"],
])
def test_malformed_payload_rejected(payload):
    with pytest.raises(MalformedMemory):
        Memory.from_payload(payload)


def test_trust_filter_excludes_superseded(tmp_path):
    s = shard_ops.open_or_create(str(tmp_path))
    good, _ = signed(value="broken")
    bad, _ = signed(value="safe to ignore")
    bad.trust_status = "superseded"
    shard_ops.upsert(s, to_points([good, bad]))
    hits, timings = shard_ops.hybrid_search(s, "dock 3 charger", filter=shard_ops.trusted_filter())
    assert [h.payload["value"] for h in hits] == ["broken"]
    unfiltered, _ = shard_ops.hybrid_search(s, "dock 3 charger")
    assert {h.payload["value"] for h in unfiltered} == {"broken", "safe to ignore"}
    assert set(timings) == {"embed_ms", "search_ms"}


def test_payload_indexes_created(tmp_path):
    s = shard_ops.open_or_create(str(tmp_path))
    assert set(s.info().payload_schema) == set(shard_ops.INDEXED_FIELDS)


def test_resolve_prefers_more_agreeing_devices():
    now = 1_700_000_000_000
    cands = {
        "safe": [{"source_device": "attacker", "ts": now}],
        "broken": [{"source_device": "robot", "ts": now}, {"source_device": "kiosk", "ts": now}],
    }
    r = resolve(cands, {"attacker": 0.9, "robot": 0.9, "kiosk": 0.8}, now_ms=now)
    assert r["winning_value"] == "broken" and r["corroborated_by"] == ["kiosk", "robot"]
    assert r["breakdown"]["broken"]["score"] == pytest.approx(0.85 * 2)


def test_resolve_ranking_independent_of_evaluation_time():
    a = {"x": [{"source_device": "robot", "ts": 0}], "y": [{"source_device": "kiosk", "ts": 3 * 86400_000}]}
    trust = {"robot": 0.9, "kiosk": 0.8}
    winners = {resolve(a, trust, now_ms=t)["winning_value"] for t in (5 * 86400_000, 60 * 86400_000, 400 * 86400_000)}
    assert len(winners) == 1
