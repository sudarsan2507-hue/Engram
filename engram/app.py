"""Device service. `create_device_app` builds one device's API; it runs either as
its own process (launch.py) or mounted under the gateway (single-port hosting)."""
import json
import os

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .device import Device, OfflineError
from .hub_client import HubClient
from .schema import SYNC

DEFAULT_DATA_ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
SEED_DIR = os.path.join(os.path.dirname(__file__), "seed_data")
BENCH_RESULTS = os.path.join(os.path.dirname(__file__), "..", "bench_results.json")


class WriteRequest(BaseModel):
    text: str
    subject: str
    value: str
    sensitivity: str = SYNC


def create_device_app(device_id: str, data_root: str, hub_url: str) -> FastAPI:
    device = Device(device_id, os.path.join(data_root, device_id))
    hub = HubClient(hub_url)
    app = FastAPI(title=f"Engram device: {device_id}")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def register() -> bool:
        try:
            hub.register(device.device_id, device.public_key)
            return True
        except Exception as e:
            device._log("hub_register_failed", error=str(e))
            return False

    def seed() -> int:
        with open(os.path.join(SEED_DIR, f"{device_id}.json")) as f:
            items = json.load(f)
        for item in items:
            device.write(**item)
        return len(items)

    app.state.device, app.state.register, app.state.seed = device, register, seed

    @app.get("/health")
    def health():
        return {"ok": True, "device_id": device_id}

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
            register()  # idempotent; a hub restart would otherwise forget this device
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
    def seed_route():
        return {"seeded": seed()}

    @app.post("/api/reset")
    def reset():
        device.reset()
        register()
        return {"ok": True}

    @app.get("/api/benchmark")
    def benchmark():
        if not os.path.exists(BENCH_RESULTS):
            return {"available": False}
        with open(BENCH_RESULTS) as f:
            return {"available": True, **json.load(f)}

    return app


def create_from_env() -> FastAPI:
    """uvicorn --factory entry point for one-process-per-device mode."""
    app = create_device_app(
        os.environ.get("ENGRAM_DEVICE_ID", "robot"),
        os.environ.get("ENGRAM_DATA_ROOT", DEFAULT_DATA_ROOT),
        os.environ.get("ENGRAM_HUB_URL", "http://127.0.0.1:8000"),
    )

    @app.on_event("startup")
    def startup():
        app.state.device.warmup()
        app.state.register()

    return app
