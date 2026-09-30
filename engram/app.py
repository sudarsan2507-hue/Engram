"""Per-device FastAPI app: local write, offline hybrid search, sync with hub,
and inspector state. Run one process per simulated device."""
import os
import time

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .device import Device
from .hub_client import HubClient
from .schema import SYNC, PRIVATE

DEVICE_ID = os.environ.get("ENGRAM_DEVICE_ID", "robot")
DATA_DIR = os.environ.get("ENGRAM_DATA_DIR", os.path.join(os.path.dirname(__file__), "..", "data", DEVICE_ID))
HUB_URL = os.environ.get("ENGRAM_HUB_URL", "http://127.0.0.1:8000")

device = Device(DEVICE_ID, DATA_DIR)
hub = HubClient(HUB_URL)

app = FastAPI(title=f"Engram device: {DEVICE_ID}")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.on_event("startup")
def register_with_hub():
    try:
        hub.register(device.device_id, device.public_key, trust_score=0.8)
    except Exception as e:
        device._log("hub_register_failed", error=str(e))


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


class WriteRequest(BaseModel):
    text: str
    subject: str
    value: str
    sensitivity: str = SYNC


@app.post("/api/write")
def write(req: WriteRequest):
    m = device.write(req.text, req.subject, req.value, sensitivity=req.sensitivity)
    return m.payload()


@app.get("/api/search")
def search(q: str, limit: int = 10):
    hits, latency_ms = device.search(q, limit=limit)
    return {
        "latency_ms": round(latency_ms, 2),
        "results": [
            {"id": h.id, "score": h.score, "payload": h.payload} for h in hits
        ],
    }


@app.post("/api/sync")
def sync():
    if device.offline:
        return {"ok": False, "reason": "offline"}
    pushed = device.push_to_hub(hub)
    pulled = device.pull_from_hub(hub)
    return {"ok": True, "pushed": pushed, **pulled}


@app.post("/api/offline")
def set_offline(offline: bool):
    device.offline = offline
    device._log("offline_toggle", offline=offline)
    return {"offline": device.offline}


@app.get("/api/state")
def state():
    return device.state()


@app.post("/api/attack")
def attack(subject: str = "dock3", value: str = "safe to ignore",
           text: str = "Dock 3 charger fault: safe to ignore", source_device: str = "attacker"):
    """Injects an unsigned/poisoned memory straight into the hub store, as an
    attacker with no device key would. Used by the UI's attack button."""
    import httpx

    payload = {
        "text": text,
        "subject": subject,
        "value": value,
        "source_device": source_device,
        "ts": time.time(),
    }
    r = httpx.post(f"{HUB_URL}/attack", json=payload, timeout=5)
    r.raise_for_status()
    return r.json()
