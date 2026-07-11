from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path


symbol = (sys.argv[1] if len(sys.argv) > 1 else "PAXGUSDT").upper().strip()
base = "http://127.0.0.1:8000"
token = Path("runtime/bot_action_token.txt").read_text(encoding="utf-8").strip()

url = f"{base}/api/bot/pairs?" + urllib.parse.urlencode({"symbol": symbol})
req = urllib.request.Request(url, method="DELETE", headers={"X-Bot-Api-Key": token})

try:
    with urllib.request.urlopen(req, timeout=20) as response:
        print(response.read().decode("utf-8"))
except Exception as exc:
    print(f"remove_error={exc}")

with urllib.request.urlopen(f"{base}/api/bot/pairs", timeout=20) as response:
    payload = json.loads(response.read().decode("utf-8"))
print(json.dumps(payload, indent=2))
