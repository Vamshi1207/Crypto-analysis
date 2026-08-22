#!/bin/sh
# Convenience wrapper. Port reclaim lives in portguard.py and runs on every
# startup, so `python -u server.py` behaves identically to this script.
set -eu
cd /app
exec python -u server.py
