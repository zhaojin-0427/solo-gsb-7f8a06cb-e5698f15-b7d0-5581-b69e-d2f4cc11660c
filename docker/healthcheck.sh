#!/bin/sh
# Container health check without extra OS packages (python is present).
python - <<'PY'
import json
import os
import urllib.request

port = os.environ.get("PORT", "8000")
try:
    with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/healthz", timeout=2) as resp:
        body = json.load(resp)
    raise SystemExit(0 if body.get("ok") else 1)
except Exception:
    raise SystemExit(1)
PY
