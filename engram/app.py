"""Per-device FastAPI service. One process per simulated device."""
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .device import Device, OfflineError
from .hub_client import HubClient
from .schema import SYNC

DEVICE_ID = os.environ.get("ENGRAM_DEVICE_ID", "robot")
DATA_ROOT = os.environ.get("ENGRAM_DATA_ROOT", os.path.join(os.path.dirname(__file__), "..", "data"))
HUB_URL = os.environ.get("ENGRAM_HUB_URL", "http://127.0.0.1:8000")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
SEED_DIR = os.path.join(os.path.dirname(__file__), "seed_data")
BENCH_RESULTS = os.path.join(os.path.dirname(__file__), "..", "bench_results.json")

device = Device(DEVICE_ID, os.path.join(DATA_ROOT, DEVICE_ID))
hub = HubClient(HUB_URL)

app = FastAPI(title=f"Engram device: {DEVICE_ID}")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


def _register():
    try:
        hub.register(device.device_id, device.public_key)
        return True
    except Exception as e:
        device._log("hub_register_failed", error=str(e))
        return False


@app.on_event("startup")
def startup():
    device.warmup()
    _register()


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return {"ok": True, "device_id": DEVICE_ID}


class WriteRequest(BaseModel):
    text: str
    subject: str
    value: str
    sensitivity: str = SYNC


@app.post("/api/write")
def write(req: WriteRequest):
    return device.write(req.text, req.subject, req.value, sensitivity=req.sensitivity).payload()


@app.get("/api/search")
def search(q: str, limit: int = 8, trusted_only: bool = True):
    hits, timings = device.search(q, limit=limit, trusted_only=trusted_only)
    return {
        **timings,
        "filter": {"must": [{"key": "trust_status", "match": "verified"}]} if trusted_only else None,
        "results": [{"id": str(h.id), "score": h.score, "payload": h.payload} for h in hits],
    }


@app.post("/api/sync")
def sync(push: bool = True, pull: bool = True):
    if device.offline:
        return {"ok": False, "reason": "offline: sync disabled, local search still works"}
    try:
        _register()  # idempotent; a hub restart would otherwise forget this device
        out = {"ok": True}
        if push:
            out["pushed"] = device.push(hub)
        if pull:
            out["pull"] = device.pull(hub)
        return out
    except OfflineError as e:
        return {"ok": False, "reason": str(e)}
    except Exception as e:
        device._log("sync_failed", error=str(e))
        return {"ok": False, "reason": f"hub unreachable: {e}"}


@app.post("/api/offline")
def set_offline(offline: bool):
    device.offline = offline
    device._log("offline_toggle", offline=offline)
    return {"offline": device.offline}


@app.post("/api/quarantine/{qid}/release")
def release(qid: str):
    return device.release(qid)


@app.get("/api/state")
def state():
    return device.state()


@app.post("/api/seed")
def seed():
    path = os.path.join(SEED_DIR, f"{DEVICE_ID}.json")
    if not os.path.exists(path):
        raise HTTPException(404, "no seed file for this device")
    with open(path) as f:
        items = json.load(f)
    for item in items:
        device.write(**item)
    return {"seeded": len(items)}


@app.post("/api/reset")
def reset():
    device.reset()
    _register()
    return {"ok": True}


@app.get("/api/benchmark")
def benchmark():
    if not os.path.exists(BENCH_RESULTS):
        return {"available": False}
    with open(BENCH_RESULTS) as f:
        return {"available": True, **json.load(f)}
