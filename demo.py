"""Scripted demo against the running services (start them with `python launch.py`).

Every step checks the actual outcome and the script exits non-zero if a
defense fails -- nothing here is printed from a script of expected results.
"""
import os
import sys

import httpx

BASE = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ENGRAM_BASE_URL", "")).rstrip("/")
if BASE:  # single-port gateway, e.g. the hosted Space: python demo.py https://<space>.hf.space
    HUB, ROBOT, KIOSK = BASE, f"{BASE}/robot", f"{BASE}/kiosk"
else:
    HUB, ROBOT, KIOSK = "http://127.0.0.1:8000", "http://127.0.0.1:8001", "http://127.0.0.1:8002"
DEVICES = {"robot": ROBOT, "kiosk": KIOSK}
http = httpx.Client(timeout=60)
failures = []


def step(title):
    print(f"\n== {title} " + "=" * max(0, 66 - len(title)))


def check(ok: bool, label: str):
    print(f"   [{'PASS' if ok else 'FAIL'}] {label}")
    if not ok:
        failures.append(label)


def post(url, **kw):
    r = http.post(url, **kw)
    r.raise_for_status()
    return r.json()


def get(url, **kw):
    r = http.get(url, **kw)
    r.raise_for_status()
    return r.json()


def state(dev):
    return get(f"{DEVICES[dev]}/api/state")


def main():
    step("0. Reset hub + both devices, load MOCK seed memories")
    post(f"{HUB}/demo/reset")
    for base in DEVICES.values():
        post(f"{base}/api/reset")
        post(f"{base}/api/seed")
    held = state("robot")["held_back"]
    for m in held:
        print(f"   robot holds back {m['subject']!r}: {m['route_reason']}")
    check(len(held) == 1, "PII router held exactly one robot memory back as PRIVATE")

    step("1. Both devices offline: sync is blocked, hybrid search still works")
    for dev, base in DEVICES.items():
        post(f"{base}/api/offline", params={"offline": True})
        blocked = post(f"{base}/api/sync")
        r = get(f"{base}/api/search", params={"q": "dock 3 charger fault"})
        top = r["results"][0]["payload"]
        print(f"   {dev}: sync -> {blocked['reason']!r}")
        print(f"   {dev}: top hit {top['subject']}={top['value']}  "
              f"(embed {r['embed_ms']} ms + vector search {r['search_ms']} ms, filter trust_status=verified)")
        check(not blocked["ok"] and top["subject"] == "dock3", f"{dev} searches offline, cannot sync")
        post(f"{base}/api/offline", params={"offline": False})

    step("2. Sync round: robot -> hub -> kiosk -> hub -> robot")
    print("   robot:", post(f"{ROBOT}/api/sync"))
    print("   kiosk:", post(f"{KIOSK}/api/sync"))
    print("   robot:", post(f"{ROBOT}/api/sync"))
    wire = get(f"{HUB}/pull", params={"since": 0})["items"]
    check(all(m["sensitivity"] == "SYNC" for m in wire) and not any("@" in m["text"] for m in wire),
          f"hub holds {len(wire)} memories, none PRIVATE")
    for dev in DEVICES:
        dock3 = [m for m in state(dev)["memories"] if m["subject"] == "dock3"]
        check(all(m["corroborated_by"] == ["kiosk", "robot"] for m in dock3),
              f"{dev}: dock3=broken corroborated by kiosk+robot")

    step("3. Genuine conflict: robot says firmware 2.3.1, kiosk says 2.3.0")
    winners = {}
    for dev in DEVICES:
        c = next(c for c in state(dev)["conflicts"] if c["subject"] == "firmware")
        winners[dev] = c["winner"]
        for value, b in c["breakdown"].items():
            print(f"   {dev}: {value:<6} score = avg_trust x decay x devices = {b['formula']} = {b['score']}")
    check(len(set(winners.values())) == 1, f"both devices converge on firmware={winners['robot']}")
    fw = [m for m in state("kiosk")["memories"] if m["subject"] == "firmware"]
    check(all(m["sig_valid"] for m in fw), "loser is marked superseded, signed content untouched")

    step("4. Six poisoning attacks, all claiming the sparking Dock 3 charger is 'safe to ignore'")
    catalog = get(f"{HUB}/demo/attacks")
    launched = {}
    for mode, meta in catalog.items():
        res = post(f"{HUB}/demo/attack/{mode}")
        launched[mode] = res["payload"]["id"]
        print(f"   {meta['title']:<38} -> {'; '.join(res['notes']) or 'injected into hub'}")
    for dev, base in DEVICES.items():
        print(f"   {dev} sync:", post(f"{base}/api/sync")["pull"])

    step("5. Where did each attack end up?")
    quarantine = {q["id"]: (dev, q["reasons"]) for dev in DEVICES for q in state(dev)["quarantine"]}
    for mode, mid in launched.items():
        dev, reasons = quarantine.get(mid, (None, None))
        check(dev is not None, f"{catalog[mode]['title']:<38} quarantined on {dev}: {reasons}")
    for dev, base in DEVICES.items():
        hits = get(f"{base}/api/search", params={"q": "is the dock 3 charger safe", "limit": 20})["results"]
        check(all(h["payload"]["value"] != "safe to ignore" for h in hits), f"{dev}: poison absent from search")

    step("6. The filter is the boundary: same query with the trust filter off")
    on = get(f"{KIOSK}/api/search", params={"q": "firmware version"})["results"]
    off = get(f"{KIOSK}/api/search", params={"q": "firmware version", "trusted_only": False})["results"]
    fmt = lambda rs: sorted({f"{r['payload']['value']}({r['payload']['trust_status']})"
                             for r in rs if r["payload"]["subject"] == "firmware"})
    print(f"   filter on : {fmt(on)}")
    print(f"   filter off: {fmt(off)}")
    check(len(fmt(off)) > len(fmt(on)), "superseded value only visible with the filter removed")

    step("Result")
    if failures:
        print(f"   {len(failures)} check(s) FAILED:")
        for f in failures:
            print(f"    - {f}")
        return 1
    print(f"   all checks passed. Inspector: {HUB}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
