"""End-to-end: hub + two devices in-process, real shards, real embeddings, real signatures."""
import pytest

from engram import crypto_utils
from engram.device import OfflineError
from engram.schema import Memory

from conftest import by_subject

EXPECTED = {
    # attack mode -> (target device, reasons that must be present)
    "unsigned": ("kiosk", {"unregistered_device", "contradicts_corroborated_memory"}),
    "impersonate": ("kiosk", {"invalid_or_missing_signature", "contradicts_corroborated_memory"}),
    "tamper": ("kiosk", {"invalid_or_missing_signature", "contradicts_corroborated_memory"}),
    "key_hijack": ("kiosk", {"invalid_or_missing_signature", "registry_key_mismatch"}),
    "sybil": ("robot", {"low_trust_source(0.30<0.5)", "contradicts_corroborated_memory"}),
    "stolen_key_evasion": ("robot", {"semantic_conflict_with_corroborated_memory"}),
}


def test_steady_state_corroboration(world):
    for d in (world.robot, world.kiosk):
        dock3 = by_subject(d, "dock3")
        assert {m["value"] for m in dock3} == {"broken"}
        assert all(m["corroborated_by"] == ["kiosk", "robot"] for m in dock3)
        assert all(m["sig_valid"] for m in d.state()["memories"])
        assert d.quarantined() == []


def test_conflict_converges_on_both_devices(world):
    world.kiosk.pull(world.hub)  # kiosk sees robot's side again only via new items; ensure final state
    winners = []
    for d in (world.robot, world.kiosk):
        fw = by_subject(d, "firmware")
        verified = {m["value"] for m in fw if m["trust_status"] == "verified"}
        superseded = {m["value"] for m in fw if m["trust_status"] == "superseded"}
        assert len(verified) == 1 and len(superseded) == 1
        winners.append(verified.pop())
        assert all(m["sig_valid"] for m in fw), "merge must never rewrite signed content"
    assert winners[0] == winners[1] == "2.3.1"


def test_superseded_hidden_by_trust_filter_only(world):
    hits, _ = world.kiosk.search("firmware version", limit=20)
    assert "2.3.0" not in {h.payload["value"] for h in hits if h.payload["subject"] == "firmware"}
    raw, _ = world.kiosk.search("firmware version", limit=20, trusted_only=False)
    assert "2.3.0" in {h.payload["value"] for h in raw if h.payload["subject"] == "firmware"}


def test_private_never_reaches_hub(world):
    wire = world.http.get("/pull", params={"since": 0}).json()["items"]
    assert all(m["sensitivity"] == "SYNC" for m in wire)
    assert not any("@" in m["text"] for m in wire)
    assert any(m["sensitivity"] == "PRIVATE" for m in world.robot.held_back())


def test_only_signed_content_goes_on_the_wire(world):
    wire = world.http.get("/pull", params={"since": 0}).json()["items"]
    assert all(set(m) == set(crypto_utils.CANONICAL_FIELDS) | {"sensitivity", "signature"} for m in wire)


def test_incremental_pull_rescreens_nothing(world):
    again = world.kiosk.pull(world.hub)
    assert again["received"] == 0


def test_offline_blocks_sync_not_search(world):
    world.robot.offline = True
    with pytest.raises(OfflineError):
        world.robot.pull(world.hub)
    with pytest.raises(OfflineError):
        world.robot.push(world.hub)
    hits, _ = world.robot.search("dock charger")
    assert hits


@pytest.mark.parametrize("mode", list(EXPECTED))
def test_attack_is_quarantined_with_reasons(world, mode):
    target_name, must_have = EXPECTED[mode]
    target = getattr(world, target_name)
    res = world.http.post(f"/demo/attack/{mode}").json()
    assert res["ok"], res
    summary = target.pull(world.hub)
    assert summary["quarantined"] == 1 and summary["promoted"] == 0
    entry = target.quarantined()[0]
    assert must_have <= set(entry["reasons"]), entry["reasons"]
    hits, _ = target.search("dock 3 charger safe")
    assert "safe to ignore" not in {h.payload["value"] for h in hits}


def test_key_hijack_reregistration_rejected(world):
    _, pk = crypto_utils.new_keypair()
    r = world.http.post("/register", json={"device_id": "robot", "public_key": pk})
    assert r.status_code == 409


def test_legit_memories_still_accepted_after_registry_compromise(world):
    world.http.post("/demo/attack/key_hijack")
    world.kiosk.pull(world.hub)
    world.robot.write("Route A reopened after cleaning", "route_a_status", "open")
    world.robot.push(world.hub)
    summary = world.kiosk.pull(world.hub)
    assert summary["promoted"] == 1 and summary["quarantined"] == 0


def test_malformed_payload_quarantined_not_crash(world):
    from engram import hub
    hub.state.append([{"id": "garbage", "text": 5}], via="attacker")
    summary = world.kiosk.pull(world.hub)
    assert summary["quarantined"] == 1
    assert world.kiosk.quarantined()[0]["reasons"] == ["malformed_payload"]


def test_release_refuses_provenance_failures(world):
    world.http.post("/demo/attack/unsigned")
    world.kiosk.pull(world.hub)
    qid = world.kiosk.quarantined()[0]["id"]
    assert not world.kiosk.release(qid)["ok"]


def test_release_allows_trust_quarantine_and_rescores(world):
    world.http.post("/demo/attack/sybil")
    world.robot.pull(world.hub)
    qid = world.robot.quarantined()[0]["id"]
    assert world.robot.release(qid)["ok"]
    dock3 = by_subject(world.robot, "dock3")
    winner = {m["value"] for m in dock3 if m["trust_status"] == "verified"}
    assert winner == {"broken"}, "released low-trust claim must still lose to the corroborated value"


def test_identity_persists_across_restart(world, tmp_path):
    from engram.device import Device
    pk = world.robot.public_key
    world.robot.trusted.close()
    world.robot.staging.close()
    again = Device("robot", str(tmp_path / "robot"))
    assert again.public_key == pk
    again.trusted.close()
    again.staging.close()
