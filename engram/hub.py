"""Cloud hub: device registry (device_id -> public_key, trust_score) plus a
dumb REST store for pushed/pulled memories. No screening happens here — the
hub is intentionally untrusted; each device screens on pull."""
import time
import uuid

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="Engram Hub")

registry: dict[str, dict] = {}
store: dict[str, dict] = {}  # memory id -> payload


class RegisterRequest(BaseModel):
    device_id: str
    public_key: str
    trust_score: float = 0.8


class PushRequest(BaseModel):
    device_id: str
    public_key: str
    memories: list[dict]


@app.post("/register")
def register(req: RegisterRequest):
    registry[req.device_id] = {
        "public_key": req.public_key,
        "trust_score": req.trust_score,
        "registered_at": time.time(),
    }
    return {"ok": True}


@app.get("/registry")
def get_registry():
    return registry


@app.post("/push")
def push(req: PushRequest):
    for m in req.memories:
        store[m["id"]] = m
    return {"ok": True, "stored": len(req.memories)}


@app.get("/pull")
def pull(exclude_device: str = ""):
    return [m for m in store.values() if m.get("source_device") != exclude_device]


@app.post("/attack")
def attack(payload: dict):
    """Inject a memory straight into the store, bypassing any device's signing.
    Used by the UI's 'attack' button to demonstrate the poison screen."""
    payload.setdefault("id", str(uuid.uuid4()))
    payload.setdefault("ts", time.time())
    payload.setdefault("sensitivity", "SYNC")
    payload.setdefault("trust_status", "pending")
    payload.setdefault("corroborated_by", [])
    payload.setdefault("quarantine_reasons", [])
    payload.setdefault("signature", "")  # deliberately unsigned
    store[payload["id"]] = payload
    return {"ok": True, "injected": payload}


@app.get("/state")
def state():
    return {"registry": registry, "store_count": len(store)}
