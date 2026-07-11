from __future__ import annotations

import json
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/bot/candidates?refresh=1", timeout=20) as response:
    data = json.loads(response.read().decode("utf-8"))

print("best=", data.get("best"))
print("top by api order:")
for item in data.get("candidates", [])[:6]:
    print(
        item.get("symbol"),
        "fee_bps=", item.get("fee_bps"),
        "fee_ok=", item.get("fee_ok"),
        "liq=", item.get("liquidity_ok"),
        "score=", item.get("score"),
    )
