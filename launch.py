"""One command to run everything: hub (:8000) + robot (:8001) + kiosk (:8002).

    python launch.py            fresh start (wipes ./data), then serve until Ctrl+C
    python launch.py --demo     fresh start, run the scripted demo, keep serving
    python launch.py --keep     keep existing ./data
"""
import argparse
import os
import shutil
import subprocess
import sys
import time

import httpx

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SERVICES = [
    ("hub", "engram.hub:app", 8000, {}),
    ("robot", "engram.app:create_from_env", 8001, {"ENGRAM_DEVICE_ID": "robot"}),
    ("kiosk", "engram.app:create_from_env", 8002, {"ENGRAM_DEVICE_ID": "kiosk"}),
]


def wait_healthy(port: int, proc, timeout: float = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep existing ./data")
    ap.add_argument("--demo", action="store_true", help="run demo.py once everything is up")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if not args.keep and os.path.isdir(DATA):
        shutil.rmtree(DATA)
    os.makedirs(DATA, exist_ok=True)

    procs = []
    try:
        for name, target, port, extra in SERVICES:
            env = {**os.environ, "ENGRAM_DATA_ROOT": DATA, "ENGRAM_HUB_URL": "http://127.0.0.1:8000",
                   "HF_HUB_DISABLE_SYMLINKS_WARNING": "1", **extra}
            log = open(os.path.join(DATA, f"{name}.log"), "w")
            factory = ["--factory"] if target.endswith("create_from_env") else []
            p = subprocess.Popen([sys.executable, "-m", "uvicorn", target, *factory, "--port", str(port),
                                  "--log-level", "warning"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
            procs.append((name, port, p))
            print(f"starting {name:<5} on :{port} ...", end=" ", flush=True)
            if not wait_healthy(port, p):
                print(f"FAILED -- see data/{name}.log")
                return 1
            print("up")

        print("\nInspector: http://127.0.0.1:8000/\n")
        if args.demo:
            subprocess.run([sys.executable, os.path.join(ROOT, "demo.py")], cwd=ROOT)
        print("Serving. Ctrl+C to stop.")
        while all(p.poll() is None for _, _, p in procs):
            time.sleep(1)
        dead = [n for n, _, p in procs if p.poll() is not None]
        print(f"process exited: {dead} -- see data/<name>.log")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        for _, _, p in procs:
            p.terminate()
        for _, _, p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    sys.exit(main())
