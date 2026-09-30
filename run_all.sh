#!/usr/bin/env bash
# Wipes ./data and starts hub + robot + kiosk in the background.
set -e
cd "$(dirname "$0")"

rm -rf data
mkdir -p data

PY=.venv/Scripts/python.exe
if [ ! -f "$PY" ]; then PY=.venv/bin/python; fi

echo "Starting hub on :8000"
ENGRAM_HUB_URL=http://127.0.0.1:8000 "$PY" -m uvicorn engram.hub:app --port 8000 --log-level warning &
echo $! > .hub.pid
sleep 2

echo "Starting robot on :8001"
ENGRAM_DEVICE_ID=robot ENGRAM_DATA_DIR=./data/robot ENGRAM_HUB_URL=http://127.0.0.1:8000 \
  "$PY" -m uvicorn engram.app:app --port 8001 --log-level warning &
echo $! > .robot.pid

echo "Starting kiosk on :8002"
ENGRAM_DEVICE_ID=kiosk ENGRAM_DATA_DIR=./data/kiosk ENGRAM_HUB_URL=http://127.0.0.1:8000 \
  "$PY" -m uvicorn engram.app:app --port 8002 --log-level warning &
echo $! > .kiosk.pid

sleep 3
echo "All up. UI: open engram/static/index.html directly, or http://127.0.0.1:8001/ and http://127.0.0.1:8002/"
echo "Run 'python demo.py' for the scripted demo, or './stop_all.sh' to stop."
