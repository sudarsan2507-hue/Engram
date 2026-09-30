"""Single-port hosting (e.g. Hugging Face Spaces): the hub at /, robot at /robot,
kiosk at /kiosk, all in one process. Devices still reach the hub over HTTP on
the same port; they share one copy of the embedding model.

    uvicorn engram.gateway:app --host 0.0.0.0 --port 7860
"""
import os
import threading
import time

from . import hub
from .app import DEFAULT_DATA_ROOT, create_device_app

PORT = int(os.environ.get("PORT", "7860"))
DATA_ROOT = os.environ.get("ENGRAM_DATA_ROOT", DEFAULT_DATA_ROOT)
HUB_URL = f"http://127.0.0.1:{PORT}"

app = hub.app
devices = {name: create_device_app(name, DATA_ROOT, HUB_URL) for name in ("robot", "kiosk")}
for name, sub in devices.items():
    app.mount(f"/{name}", sub)
hub.DEVICE_URLS = {name: f"/{name}" for name in devices}


def _bootstrap():
    """Registration needs the server to be accepting connections, so it runs after startup.
    Devices are seeded on first boot so a visitor never lands on an empty demo."""
    for _ in range(120):
        if all(sub.state.register() for sub in devices.values()):
            break
        time.sleep(0.5)
    for sub in devices.values():
        if sub.state.device.stats()["total"] == 0:
            sub.state.seed()


@app.on_event("startup")
def startup():
    next(iter(devices.values())).state.device.warmup()
    threading.Thread(target=_bootstrap, daemon=True).start()
