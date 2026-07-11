from __future__ import annotations

import logging
import os
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

logging.basicConfig(
    level=os.environ.get("BOT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

STOP_FLAG = ROOT_DIR / "runtime" / "application_stopped.flag"

if STOP_FLAG.exists():
    def application(environ, start_response):
        body = b"BitRucker application is stopped. Trading automation is disabled."
        start_response(
            "503 Service Unavailable",
            [
                ("Content-Type", "text/plain; charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ],
        )
        return [body]

    app = application
else:
    from binance_bot.dashboard import create_dashboard_app

    application = create_dashboard_app(ROOT_DIR)
    app = application

    # When wfastcgi imports this module, also start Waitress in background.
    # Waitress stays stable even when wfastcgi crashes, fixing the intermittent
    # OSError [Errno 22] FastCGI transport crash.
    import threading

    def _start_waitress():
        import time
        time.sleep(2)  # let wfastcgi settle first
        try:
            from waitress import serve
            serve(application, host="127.0.0.1", port=8050, threads=4, _quiet=True)
        except Exception:
            pass

    if not os.environ.get("WAITRESS_RUNNING"):
        os.environ["WAITRESS_RUNNING"] = "1"
        t = threading.Thread(target=_start_waitress, daemon=True)
        t.start()