import gc
import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from engram import attacks, hub  # noqa: E402
from engram.device import Device  # noqa: E402
from engram.hub_client import HubClient  # noqa: E402

SEED = ROOT / "engram" / "seed_data"


@pytest.fixture(autouse=True)
def _free_disk(tmp_path):
    """Each EdgeShard pre-allocates ~169 MB on disk; delete every test's shards as soon as it ends."""
    yield
    gc.collect()
    shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def world(tmp_path):
    """Hub + robot + kiosk wired in-process, seeded and synced into the demo's steady state:
    dock3=broken corroborated by both devices, firmware conflict resolved."""
    hub.state.reset()
    attacks.DATA_ROOT = str(tmp_path)
    client = HubClient(client=TestClient(hub.app))
    robot = Device("robot", str(tmp_path / "robot"))
    kiosk = Device("kiosk", str(tmp_path / "kiosk"))
    for d in (robot, kiosk):
        client.register(d.device_id, d.public_key)
        for item in json.loads((SEED / f"{d.device_id}.json").read_text()):
            d.write(**item)
    robot.push(client)
    kiosk.pull(client)
    kiosk.push(client)
    robot.pull(client)

    class World:
        pass

    w = World()
    w.hub, w.robot, w.kiosk, w.http = client, robot, kiosk, TestClient(hub.app)
    yield w
    for d in (robot, kiosk):
        for s in (d.trusted, d.staging):
            try:
                s.close()
            except Exception:
                pass


def by_subject(device, subject):
    return [m for m in device.state()["memories"] if m["subject"] == subject]
