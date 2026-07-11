from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

symbol = (sys.argv[1] if len(sys.argv) > 1 else "LINKFDUSD").upper().strip()
base = "http://127.0.0.1:8000"
token = Path("runtime/bot_action_token.txt").read_text(encoding="utf-8").strip()

req = urllib.request.Request(
    f"{base}/api/bot/pairs",
    method="POST",
    headers={"Content-Type": "application/json", "X-Bot-Api-Key": token},
    data=json.dumps({"symbol": symbol}).encode("utf-8"),
)

with urllib.request.urlopen(req, timeout=30) as response:
    print(response.read().decode("utf-8"))

with urllib.request.urlopen(f"{base}/api/bot/pairs", timeout=20) as response:
    print(response.read().decode("utf-8"))
