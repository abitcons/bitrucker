from __future__ import annotations

import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/bot/pairs", timeout=20) as response:
    pairs = json.loads(response.read().decode("utf-8"))

# Use first active symbol for status call
active_symbols = [item.get("symbol") for item in pairs.get("pairs", []) if item.get("symbol")]
status_symbol = active_symbols[0] if active_symbols else "LINKUSDT"

with urllib.request.urlopen(f"http://127.0.0.1:8000/api/bot/status?symbol={status_symbol}", timeout=20) as response:
    status = json.loads(response.read().decode("utf-8"))

print("active_pairs=", active_symbols)
print("supported_symbols=", status.get("supported_symbols"))
