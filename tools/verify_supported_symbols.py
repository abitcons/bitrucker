from __future__ import annotations

import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/bot/status?symbol=LINKUSDT", timeout=20) as response:
    status = json.loads(response.read().decode("utf-8"))

print("symbol=", status.get("symbol"))
print("supported=", status.get("supported_symbols"))
