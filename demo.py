"""Scripted demo. Requires hub (:8000), robot (:8001), kiosk (:8002) already
running against empty ./data dirs (see README `run` section).

Sequence: offline search on both -> robot syncs (PRIVATE stays local, SYNC
drains) -> attack injects unsigned poisoned memory into the hub -> kiosk pulls
and quarantines it with reasons -> robot and kiosk hold a genuine firmware
version conflict -> corroboration merge resolves it with the score math shown.
"""
import json
import os
import time

import httpx

ROBOT = "http://127.0.0.1:8001"
KIOSK = "http://127.0.0.1:8002"
HUB = "http://127.0.0.1:8000"


def load_seed(name):
    with open(os.path.join(os.path.dirname(__file__), "engram", "seed_data", f"{name}.json")) as f:
        return json.load(f)


def step(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def write_all(base, items):
    for item in items:
        httpx.post(f"{base}/api/write", json=item, timeout=10).raise_for_status()


def main():
    step("1. Seeding robot and kiosk with local memories")
    write_all(ROBOT, load_seed("robot"))
    write_all(KIOSK, load_seed("kiosk"))
    print("robot + kiosk seeded.")

    step("2. Offline hybrid search on both devices (no hub calls)")
    for base, label, q in [(ROBOT, "robot", "dock charger fault"), (KIOSK, "kiosk", "firmware version")]:
        r = httpx.get(f"{base}/api/search", params={"q": q}, timeout=10).json()
        print(f"[{label}] query={q!r} latency={r['latency_ms']}ms hits={len(r['results'])}")
        for hit in r["results"][:3]:
            print(f"   {hit['score']:.3f}  {hit['payload']['subject']} = {hit['payload']['value']}")

    step("3. Robot syncs with hub (PRIVATE memory must not leave the device)")
    sync_res = httpx.post(f"{ROBOT}/api/sync", timeout=15).json()
    print("robot sync result:", sync_res)
    hub_state = httpx.get(f"{HUB}/state", timeout=10).json()
    print("hub store count after robot sync:", hub_state["store_count"])
    private_leaked = any(
        "email" in m.get("text", "") or "@" in m.get("value", "")
        for m in httpx.get(f"{HUB}/pull", timeout=10).json()
    )
    print("PRIVATE memory leaked to hub?", private_leaked, "(must be False)")

    step("4. Attacker injects an unsigned poisoned memory directly into the hub")
    attack_res = httpx.post(f"{ROBOT}/api/attack", timeout=10).json()
    print("injected:", attack_res["injected"]["subject"], "=", attack_res["injected"]["value"],
          "from", attack_res["injected"]["source_device"])

    step("5. Kiosk syncs -> poison screen runs on pull")
    kiosk_sync = httpx.post(f"{KIOSK}/api/sync", timeout=15).json()
    print("kiosk sync result:", kiosk_sync)
    q = httpx.get(f"{KIOSK}/api/state", timeout=10).json()["quarantine"]
    latest = q[0] if q else None
    if latest:
        print(f"quarantined: {latest['payload']['subject']} = {latest['payload']['value']!r} "
              f"from {latest['payload']['source_device']} -- reasons: {latest['reasons']}")
    else:
        print("nothing quarantined (unexpected)")

    step("6. Robot syncs -> pulls kiosk's firmware claim -> genuine conflict -> corroboration merge")
    robot_sync = httpx.post(f"{ROBOT}/api/sync", timeout=15).json()
    print("robot sync result:", robot_sync)
    robot_state = httpx.get(f"{ROBOT}/api/state", timeout=10).json()
    firmware = [m for m in robot_state["memories"] if m["subject"] == "firmware"]
    for m in firmware:
        print(f"firmware = {m['value']}  corroborated_by={m['corroborated_by']}")
        if m.get("score_breakdown"):
            print("  score breakdown:", json.dumps(m["score_breakdown"], indent=2))

    step("Demo complete")


if __name__ == "__main__":
    main()
