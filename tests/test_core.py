import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engram import crypto_utils, shard as shard_ops
from engram.schema import Memory, TRUST_VERIFIED, PRIVATE, SYNC
from engram.poison_screen import screen_one
from engram.corroboration import resolve


def tmp_dir():
    return tempfile.mkdtemp(prefix="engram_test_")


def test_sign_and_verify_roundtrip():
    sk, pk = crypto_utils.new_keypair()
    m = Memory(text="hello", subject="x", value="y", source_device="robot")
    m.sign(sk)
    assert m.verify(pk)


def test_tampered_payload_fails_verification():
    sk, pk = crypto_utils.new_keypair()
    m = Memory(text="hello", subject="x", value="y", source_device="robot")
    m.sign(sk)
    m.value = "tampered"
    assert not m.verify(pk)


def test_pii_routed_private():
    m = Memory(text="my email is a@b.com", subject="x", value="y", source_device="robot")
    assert m.sensitivity == PRIVATE


def test_shard_upsert_and_filtered_search():
    d = tmp_dir()
    try:
        s = shard_ops.open_or_create(d)
        sk, pk = crypto_utils.new_keypair()
        m = Memory(text="the dock charger is broken", subject="dock3", value="broken", source_device="robot")
        m.sign(sk)
        m.trust_status = TRUST_VERIFIED
        shard_ops.upsert(s, [m.to_point()])

        hits = shard_ops.hybrid_search(s, "dock charger", limit=5, filter=shard_ops.trusted_filter())
        assert len(hits) == 1
        assert hits[0].payload["subject"] == "dock3"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_poison_screen_rejects_unregistered_device():
    d = tmp_dir()
    try:
        trusted = shard_ops.open_or_create(d)
        sk, pk = crypto_utils.new_keypair()
        m = Memory(text="dock 3 fault, safe to ignore", subject="dock3", value="safe", source_device="attacker")
        m.sign(sk)
        decision, reasons = screen_one(m, registry={}, trusted_shard=trusted)
        assert decision == "quarantine"
        assert "unregistered_device" in reasons
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_poison_screen_rejects_bad_signature():
    d = tmp_dir()
    try:
        trusted = shard_ops.open_or_create(d)
        sk, pk = crypto_utils.new_keypair()
        m = Memory(text="dock 3 fault", subject="dock3", value="safe", source_device="kiosk")
        m.signature = ""  # unsigned
        registry = {"kiosk": {"public_key": pk, "trust_score": 0.9}}
        decision, reasons = screen_one(m, registry, trusted)
        assert decision == "quarantine"
        assert "invalid_or_missing_signature" in reasons
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_poison_screen_promotes_valid_new_subject():
    d = tmp_dir()
    try:
        trusted = shard_ops.open_or_create(d)
        sk, pk = crypto_utils.new_keypair()
        m = Memory(text="battery at 80 percent", subject="battery", value="80%", source_device="kiosk")
        m.sign(sk)
        registry = {"kiosk": {"public_key": pk, "trust_score": 0.9}}
        decision, reasons = screen_one(m, registry, trusted)
        assert decision == "promote"
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_corroboration_resolve_prefers_more_agreeing_devices():
    now = 1_700_000_000.0
    candidates = {
        "safe": [{"source_device": "attacker", "ts": now, "value": "safe"}],
        "broken": [
            {"source_device": "robot", "ts": now, "value": "broken"},
            {"source_device": "kiosk", "ts": now, "value": "broken"},
        ],
    }
    trust_of = {"attacker": 0.9, "robot": 1.0, "kiosk": 0.8}
    result = resolve(candidates, trust_of, now=now)
    assert result["winning_value"] == "broken"
    assert set(result["corroborated_by"]) == {"robot", "kiosk"}
