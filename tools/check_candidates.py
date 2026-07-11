from __future__ import annotations

import json
import time
import urllib.request


time.sleep(1.2)
with urllib.request.urlopen("http://127.0.0.1:8000/api/bot/candidates?refresh=1", timeout=20) as response:
    data = json.loads(response.read().decode("utf-8"))

print("best=", data.get("best"))
print("top_candidates=")
for item in data.get("candidates", [])[:5]:
    print(item)
