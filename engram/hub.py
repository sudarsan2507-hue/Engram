"""Cloud hub: device registry + append-only memory log.

The hub is deliberately *not* trusted by devices. It never screens content;
every device screens what it pulls. What the hub does enforce:
  - trust scores come from static operator config, never from the device
  - first registration pins a device's key; re-registering with another key is 409
  - the log is append-only; a memory id is stored once (first write wins)
"""
import json
import os
import threading
import time

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import attacks

CONFIG_PATH = os.environ.get("ENGRAM_HUB_CONFIG", os.path.join(os.path.dirname(__file__), "hub_config.json"))
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


class HubState:
    def __init__(self):
        with open(CONFIG_PATH) as f:
            self.config = json.load(f)
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.registry: dict[str, dict] = {}
        self.log: list[dict] = []
        self.ids: set[str] = set()
        self.events: list[dict] = []
        self.attacks: list[dict] = []

    def trust_for(self, device_id: str) -> float:
        return self.config["trust_scores"].get(device_id, self.config["default_trust"])

    def register(self, device_id: str, public_key: str) -> dict:
        with self.lock:
            existing = self.registry.get(device_id)
            if existing and existing["public_key"] != public_key:
                self.event("register_rejected", device_id=device_id, why="key already pinned")
                raise HTTPException(409, f"device '{device_id}' is already registered with a different key")
            if not existing:
                self.registry[device_id] = {"public_key": public_key, "trust_score": self.trust_for(device_id),
                                            "registered_at": int(time.time() * 1000)}
                self.event("register", device_id=device_id, trust_score=self.trust_for(device_id))
            return self.registry[device_id]

    def append(self, memories: list[dict], via: str) -> int:
        stored = 0
        with self.lock:
            for m in memories:
                mid = m.get("id") if isinstance(m, dict) else None
                key = mid if isinstance(mid, str) else json.dumps(m, sort_keys=True, default=str)
                if key in self.ids:
                    continue
                self.ids.add(key)
                self.log.append({"seq": len(self.log) + 1, "via": via, "memory": m})
                stored += 1
        return stored

    def since(self, seq: int, exclude_device: str) -> dict:
        with self.lock:
            items = [e["memory"] for e in self.log[seq:]
                     if not (isinstance(e["memory"], dict) and e["memory"].get("source_device") == exclude_device)]
            return {"items": items, "next_since": len(self.log)}

    def event(self, kind: str, **data):
        self.events.append({"ts": int(time.time() * 1000), "event": kind, **data})


state = HubState()
app = FastAPI(title="Engram Hub")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class RegisterRequest(BaseModel):
    device_id: str
    public_key: str


class PushRequest(BaseModel):
    device_id: str
    memories: list[dict]


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
def health():
    return {"ok": True}


DEVICE_URLS = {"robot": "http://127.0.0.1:8001", "kiosk": "http://127.0.0.1:8002"}


@app.get("/config")
def config():
    """Where the inspector should reach each device (separate ports locally, sub-paths when hosted)."""
    return {"devices": DEVICE_URLS}


@app.post("/register")
def register(req: RegisterRequest):
    return state.register(req.device_id, req.public_key)


@app.get("/registry")
def registry():
    return state.registry


@app.post("/push")
def push(req: PushRequest):
    stored = state.append(req.memories, via=req.device_id)
    state.event("push", device_id=req.device_id, received=len(req.memories), stored=stored)
    return {"stored": stored}


@app.get("/pull")
def pull(since: int = 0, exclude_device: str = ""):
    return state.since(since, exclude_device)


@app.get("/state")
def hub_state():
    return {
        "registry": state.registry,
        "log_size": len(state.log),
        "log": [{"seq": e["seq"], "via": e["via"],
                 "subject": e["memory"].get("subject") if isinstance(e["memory"], dict) else None,
                 "source_device": e["memory"].get("source_device") if isinstance(e["memory"], dict) else None,
                 "sensitivity": e["memory"].get("sensitivity") if isinstance(e["memory"], dict) else None}
                for e in state.log[-60:]][::-1],
        "events": state.events[-40:][::-1],
        "attacks": state.attacks,
        "config": state.config,
    }


# ---- demo control plane: attack simulation + reset. Would not exist in production. ----

@app.get("/demo/attacks")
def list_attacks():
    return attacks.CATALOG


@app.post("/demo/attack/{mode}")
def run_attack(mode: str):
    if mode not in attacks.CATALOG:
        raise HTTPException(404, f"unknown attack '{mode}'")
    result = attacks.run(mode, state)
    state.event("attack", **{k: v for k, v in result.items() if k != "payload"})
    if result.get("ok"):
        state.attacks.append({"mode": mode, "title": attacks.CATALOG[mode]["title"], "id": result["payload"]["id"],
                              "source_device": result["payload"]["source_device"], "ts": int(time.time() * 1000)})
    return result


@app.post("/demo/reset")
def reset():
    state.reset()
    return {"ok": True}
