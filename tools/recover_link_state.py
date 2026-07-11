from __future__ import annotations

import json
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"
TOKEN = Path("runtime/bot_action_token.txt").read_text(encoding="utf-8").strip()
HEADERS = {
    "Content-Type": "application/json",
    "X-Bot-Api-Key": TOKEN,
}


def post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=HEADERS,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


try:
    print("restart=", post("/api/bot/restart", {"symbol": "LINKUSDT"}))
except Exception as exc:
    print("restart_error=", exc)

try:
    print("start=", post("/api/bot/start", {"symbol": "LINKUSDT", "budget": 5000, "duration_hours": 12}))
except Exception as exc:
    print("start_error=", exc)

with urllib.request.urlopen(f"{BASE}/api/bot/status?symbol=LINKUSDT", timeout=30) as response:
    status = json.loads(response.read().decode("utf-8"))

keys = [
    "symbol",
    "status",
    "twap_status",
    "last_exit_reason",
    "advisor_summary",
    "user_requested_stop",
    "budget_total",
    "budget_remaining",
    "total_asset_summary",
]
print("final_status=", {k: status.get(k) for k in keys})
