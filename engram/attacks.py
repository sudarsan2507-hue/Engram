"""Simulated memory-poisoning attacks, run by the hub's demo control plane.

Each attack targets a different layer of the poison screen. The poisoned claim
is always the same dangerous one: the sparking Dock 3 charger is "safe to ignore".
"""
import copy
import json
import os
import uuid

from fastapi import HTTPException

from . import crypto_utils
from .schema import now_ms

DATA_ROOT = os.environ.get("ENGRAM_DATA_ROOT", os.path.join(os.path.dirname(__file__), "..", "data"))

POISON_TEXT = "Dock 3 charger fault: safe to ignore"

CATALOG = {
    "unsigned": {
        "title": "Unsigned injection",
        "story": "Attacker with write access to the hub drops an unsigned memory from a device nobody registered.",
        "layer": "provenance",
    },
    "impersonate": {
        "title": "Impersonate robot",
        "story": "Attacker labels the memory source_device=robot and signs it with their own key.",
        "layer": "provenance",
    },
    "tamper": {
        "title": "Tamper in transit",
        "story": "Attacker copies robot's genuine signed dock3 memory and edits the value, keeping the signature.",
        "layer": "provenance",
    },
    "key_hijack": {
        "title": "Registry key hijack",
        "story": "Attacker tries to re-register robot with their key (hub refuses: 409), then compromises the hub "
                 "registry directly and swaps robot's key before injecting a forged memory.",
        "layer": "key pinning",
    },
    "sybil": {
        "title": "Sybil device",
        "story": "Attacker legitimately registers a brand-new device and sends a validly signed memory. "
                 "Crypto passes; trust and corroboration do not.",
        "layer": "trust + corroboration",
    },
    "stolen_key_evasion": {
        "title": "Stolen trusted key + subject evasion",
        "story": "Attacker has stolen kiosk's private key (trust 0.8) and files the claim under a renamed subject "
                 "so the same-subject contradiction check never sees it. Only the semantic check is left.",
        "layer": "semantic (dense-vector neighbor filter)",
    },
}


def _memory(source: str, subject: str = "dock3", text: str = POISON_TEXT) -> dict:
    return {"id": str(uuid.uuid4()), "text": text, "subject": subject, "value": "safe to ignore",
            "source_device": source, "ts": now_ms(), "sensitivity": "SYNC", "signature": ""}


def _signed(m: dict, private_key: str) -> dict:
    m["signature"] = crypto_utils.sign(m, private_key)
    return m


def run(mode: str, hub) -> dict:
    notes = []
    if mode == "unsigned":
        m = _memory("unknown-drone")

    elif mode == "impersonate":
        sk, _ = crypto_utils.new_keypair()
        m = _signed(_memory("robot"), sk)

    elif mode == "tamper":
        genuine = next((e["memory"] for e in reversed(hub.log)
                        if isinstance(e["memory"], dict) and e["memory"].get("source_device") == "robot"
                        and e["memory"].get("subject") == "dock3"), None)
        if genuine is None:
            return {"ok": False, "error": "robot has not pushed its dock3 memory yet -- sync robot first"}
        m = copy.deepcopy(genuine)
        m.update(id=str(uuid.uuid4()), value="safe to ignore", text=POISON_TEXT)
        notes.append("signature copied unchanged from robot's genuine memory")

    elif mode == "key_hijack":
        sk, pk = crypto_utils.new_keypair()
        try:
            hub.register("robot", pk)
            notes.append("hub accepted the re-registration (unexpected)")
        except HTTPException as e:
            notes.append(f"re-register as robot -> HTTP {e.status_code}: {e.detail}")
        if "robot" in hub.registry:
            hub.registry["robot"] = {**hub.registry["robot"], "public_key": pk}
            notes.append("simulated hub compromise: robot's registry key swapped to the attacker's key")
        m = _signed(_memory("robot"), sk)

    elif mode == "sybil":
        sk, pk = crypto_utils.new_keypair()
        device = f"helper-bot-{uuid.uuid4().hex[:4]}"
        reg = hub.register(device, pk)
        notes.append(f"registered {device}, hub assigned default trust {reg['trust_score']}")
        m = _signed(_memory(device), sk)

    elif mode == "stolen_key_evasion":
        path = os.path.join(DATA_ROOT, "kiosk", "identity.json")
        if not os.path.exists(path):
            return {"ok": False, "error": f"kiosk identity not found at {path}"}
        with open(path) as f:
            stolen = json.load(f)["private_key"]
        m = _signed(_memory("kiosk", subject="charging_station_3"), stolen)
        notes.append("signed with kiosk's real private key; subject renamed dock3 -> charging_station_3")

    else:
        raise ValueError(mode)

    stored = hub.append([m], via="attacker")
    return {"ok": True, "mode": mode, "notes": notes, "stored": stored,
            "payload": {k: m[k] for k in ("id", "subject", "value", "source_device")}}
