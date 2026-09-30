#!/usr/bin/env bash
cd "$(dirname "$0")"
for f in .hub.pid .robot.pid .kiosk.pid; do
  if [ -f "$f" ]; then
    kill "$(cat "$f")" 2>/dev/null || true
    rm -f "$f"
  fi
done
echo "stopped."
