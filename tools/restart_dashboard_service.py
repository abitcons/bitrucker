from __future__ import annotations

import os
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOKEN_FILE = ROOT / "runtime" / "bot_action_token.txt"

try:
    output = subprocess.check_output(
        [
            "wmic",
            "process",
            "where",
            "name='python.exe'",
            "get",
            "ProcessId,CommandLine",
            "/FORMAT:csv",
        ],
        text=True,
        errors="ignore",
    )
except Exception:
    output = ""

for line in output.splitlines():
    if "src.binance_bot" not in line:
        continue
    parts = [item.strip() for item in line.split(",") if item.strip()]
    if not parts:
        continue
    pid = parts[-1]
    if pid.isdigit():
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, text=True)

env = os.environ.copy()
web_config = ROOT / "web.config"
if web_config.exists():
    try:
        root = ET.parse(web_config).getroot()
        for item in root.findall("./appSettings/add"):
            key = item.attrib.get("key")
            value = item.attrib.get("value")
            if key and value is not None:
                env[key] = value
    except Exception:
        pass
if TOKEN_FILE.exists():
    env["BOT_ACTION_API_TOKEN"] = TOKEN_FILE.read_text(encoding="utf-8").strip()
env["BOT_ENFORCE_LOCAL_ACTION_AUTH"] = "true"
env["BOT_TRUST_CF_ACCESS_HEADERS"] = "true"
env["BOT_REQUIRE_CF_ACCESS"] = "true"
env["BOT_AD_USERS_ONLY"] = "true"

subprocess.Popen(
    [
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        "-m",
        "src.binance_bot",
        "dashboard",
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
    ],
    cwd=str(ROOT),
    env=env,
)

print("dashboard-restarted")
