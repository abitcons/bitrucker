from __future__ import annotations

import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/bot/candidates?refresh=1", timeout=25) as response:
    data = json.loads(response.read().decode("utf-8"))

symbols = [item.get("symbol") for item in data.get("candidates", [])]
fdusd = [s for s in symbols if isinstance(s, str) and s.endswith("FDUSD")]

print("fdusd_candidates=", fdusd)
print("best=", data.get("best"))
