#!/usr/bin/env python
"""
Launch the bot dashboard with Cloudflare Tunnel for remote access.
Run: python launch_with_tunnel.py [--dashboard-port 8000] [--tunnel-name my-bot]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
LOGGER = logging.getLogger(__name__)


def find_cloudflared() -> str:
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\cloudflared\cloudflared.exe"),
        r"C:\Program Files\cloudflared\cloudflared.exe",
        r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
        str(Path(__file__).parent / "cloudflared.exe"),
        str(Path(__file__).parent.parent / "cloudflared.exe"),
    ]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "cloudflared not found. Install from https://github.com/cloudflare/cloudflared/releases"
    )


def launch_dashboard(root_dir: Path, port: int) -> subprocess.Popen:
    """Launch the Flask dashboard in a subprocess."""
    LOGGER.info("Launching dashboard on port %d...", port)
    cmd = [sys.executable, "main.py", "dashboard", "--host", "127.0.0.1", "--port", str(port)]
    return subprocess.Popen(cmd, cwd=root_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def launch_tunnel(port: int, tunnel_name: str) -> tuple[subprocess.Popen, str]:
    """Launch Cloudflare Tunnel and return the public URL."""
    cloudflared_exe = find_cloudflared()
    LOGGER.info("Launching Cloudflare Tunnel to expose localhost:%d...", port)
    cmd = [
        cloudflared_exe,
        "tunnel",
        "run",
        "--url",
        f"http://127.0.0.1:{port}",
        "--name",
        tunnel_name,
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    url = _wait_for_tunnel_url(proc, timeout_seconds=15)
    if not url:
        raise RuntimeError("Failed to get tunnel URL from cloudflared")
    LOGGER.info("🌐 Public dashboard URL: %s", url)
    return proc, url


def _wait_for_tunnel_url(proc: subprocess.Popen, timeout_seconds: int = 15) -> str | None:
    """Poll stderr/stdout for the tunnel URL within the timeout."""
    start = time.time()
    while time.time() - start < timeout_seconds:
        if proc.poll() is not None:
            stderr = proc.stderr.read() if proc.stderr else ""
            stdout = proc.stdout.read() if proc.stdout else ""
            LOGGER.error("cloudflared exited early: %s %s", stdout, stderr)
            return None
        try:
            line = proc.stderr.readline() if proc.stderr else ""
            if line and ("cfd.sh" in line or "https://" in line):
                parts = line.split()
                for part in parts:
                    if "cfd.sh" in part or part.startswith("https://"):
                        return part.rstrip()
            line = proc.stdout.readline() if proc.stdout else ""
            if line and ("cfd.sh" in line or "https://" in line):
                parts = line.split()
                for part in parts:
                    if "cfd.sh" in part or part.startswith("https://"):
                        return part.rstrip()
        except Exception:
            pass
        time.sleep(0.5)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch bot dashboard with Cloudflare Tunnel")
    parser.add_argument("--dashboard-port", type=int, default=8000, help="Local dashboard port")
    parser.add_argument(
        "--tunnel-name",
        default="binance-bot",
        help="Tunnel name prefix (Cloudflare will assign a unique subdomain)",
    )
    args = parser.parse_args(argv)

    root_dir = Path(__file__).resolve().parent
    dashboard_proc = None
    tunnel_proc = None
    try:
        dashboard_proc = launch_dashboard(root_dir, args.dashboard_port)
        time.sleep(2)
        tunnel_proc, public_url = launch_tunnel(args.dashboard_port, args.tunnel_name)
        LOGGER.info("✅ Bot dashboard is live at: %s", public_url)
        LOGGER.info("Press Ctrl+C to stop")
        while True:
            time.sleep(1)
            if dashboard_proc.poll() is not None:
                LOGGER.error("Dashboard process exited unexpectedly")
                return 1
            if tunnel_proc.poll() is not None:
                LOGGER.error("Tunnel process exited unexpectedly")
                return 1
    except KeyboardInterrupt:
        LOGGER.info("Shutting down...")
    except Exception as exc:
        LOGGER.exception("Error: %s", exc)
        return 1
    finally:
        if dashboard_proc and dashboard_proc.poll() is None:
            dashboard_proc.terminate()
            try:
                dashboard_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dashboard_proc.kill()
        if tunnel_proc and tunnel_proc.poll() is None:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tunnel_proc.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
