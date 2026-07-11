"""Background Codex strategy daemon.

Runs as the logged-in user so Codex CLI can reuse the user's saved ChatGPT
authentication. The bot hosted in IIS writes JSON request files and reads the
JSON response files written by this daemon.

Usage:
    python tools/codex_daemon.py

Install as a scheduled task:
    python tools/codex_daemon.py --install
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUESTS_DIR = ROOT / "runtime" / "codex_requests"
RESPONSES_DIR = ROOT / "runtime" / "codex_responses"
LOG_FILE = ROOT / "runtime" / "codex-daemon.log"
LOCK_FILE = ROOT / "runtime" / "codex-daemon.lock"
BUDGET_FILE = ROOT / "runtime" / "codex-credit-budget.json"
CONTROL_FILE = ROOT / "runtime" / "codex-control.json"
CODEX_WORKSPACE = ROOT / "runtime" / "codex_cli_workspace"

ALLOWED_MODELS = {
    "gpt-5.4-mini",
    "gpt-5.4",
    "gpt-5.5",
    "gpt-5.6-luna",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.3-codex-spark",
}

TASK_NAME = "BitRuckerCodexDaemon"
_LOCK_HANDLE: Any | None = None

DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "maxLength": 32},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "target_profit_usdt": {"type": "number", "minimum": 3, "maximum": 200},
        "target_pct": {"type": "number", "minimum": 0.003, "maximum": 0.035},
        "reason": {"type": "string", "maxLength": 160},
        "regime": {"type": "string", "maxLength": 32},
        "dca_levels": {"type": "integer", "minimum": 1, "maximum": 8},
        "dca_min_step_pct": {"type": "number", "minimum": 0.001, "maximum": 0.05},
        "dca_max_step_pct": {"type": "number", "minimum": 0.001, "maximum": 0.05},
        "dca_reprice_interval_seconds": {"type": "integer", "minimum": 60, "maximum": 600},
        "max_entry_balance_fraction": {"type": "number", "minimum": 0.01, "maximum": 0.2},
        "stop_loss_pct": {"type": "number", "minimum": 0.005, "maximum": 0.05},
        "strategy_mode": {"type": "string", "enum": ["swing", "momentum", "defensive", "mean_reversion"]},
    },
    "required": [
        "action",
        "confidence",
        "target_profit_usdt",
        "target_pct",
        "reason",
        "regime",
        "dca_levels",
        "dca_min_step_pct",
        "dca_max_step_pct",
        "dca_reprice_interval_seconds",
        "max_entry_balance_fraction",
        "stop_loss_pct",
        "strategy_mode",
    ],
    "additionalProperties": False,
}


def _log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _load_web_config() -> None:
    web_config = ROOT / "web.config"
    if not web_config.exists():
        return
    try:
        root = ET.parse(web_config).getroot()
        for item in root.findall("./appSettings/add"):
            key = item.attrib.get("key")
            value = item.attrib.get("value")
            if key and value is not None:
                os.environ.setdefault(key, value)
    except Exception as exc:
        _log(f"web.config load warning: {exc}")


def _vscode_codex_candidates() -> list[str]:
    extensions_dir = Path.home() / ".vscode" / "extensions"
    if not extensions_dir.exists():
        return []
    matches = list(extensions_dir.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"))
    matches.sort(key=lambda path: path.stat().st_mtime if path.exists() else 0, reverse=True)
    return [str(path) for path in matches]


def _get_binary() -> str | None:
    candidates = [
        os.environ.get("CODEX_CLI_BINARY", "").strip(),
        shutil.which("codex") or "",
        str(Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"),
        *_vscode_codex_candidates(),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        candidate_path = Path(candidate)
        if candidate_path.exists():
            return str(candidate_path)
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return None


def _ps_quote_path(path: str | Path) -> str:
    return str(path).replace("'", "''")


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10)
    except Exception:
        pass


def _run_powershell(script_text: str, timeout: int) -> tuple[str, int]:
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix="-codex-launcher.ps1", delete=False, encoding="utf-8") as tmp:
        tmp.write(script_text)
        script_file = tmp.name

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                script_file,
            ],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return (stdout or "") + "\n" + (stderr or ""), proc.returncode
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            proc.communicate()
            raise
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
            try:
                proc.communicate(timeout=5)
            except Exception:
                pass
        Path(script_file).unlink(missing_ok=True)


def _load_runtime_control() -> dict[str, str | bool]:
    default_model = os.environ.get("CODEX_CLI_MODEL", "gpt-5.4-mini").strip() or "gpt-5.4-mini"
    if default_model not in ALLOWED_MODELS:
        default_model = "gpt-5.4-mini"
    control: dict[str, str | bool] = {
        "enabled": os.environ.get("CODEX_CLI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"},
        "model": default_model,
        "reasoning_effort": os.environ.get("CODEX_CLI_REASONING_EFFORT", "low").strip().lower(),
    }
    try:
        data = json.loads(CONTROL_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if isinstance(data.get("enabled"), bool):
                control["enabled"] = data["enabled"]
            if str(data.get("model", "")) in ALLOWED_MODELS:
                control["model"] = str(data["model"])
            if str(data.get("reasoning_effort", "")) in {"minimal", "low", "medium", "high"}:
                control["reasoning_effort"] = str(data["reasoning_effort"])
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return control


def _call_codex(binary: str, prompt_text: str, timeout: int = 60, model: str | None = None, reasoning_effort: str | None = None) -> tuple[str, int]:
    """Invoke Codex non-interactively and return combined output plus exit code."""
    import tempfile

    prompt = f"Return one JSON strategy decision from this data only. Do not use tools or files.\n{prompt_text}"
    with tempfile.NamedTemporaryFile(mode="w", suffix="-codex-prompt.txt", delete=False, encoding="utf-8") as tmp_p:
        tmp_p.write(prompt)
        prompt_file = tmp_p.name
    with tempfile.NamedTemporaryFile(mode="w", suffix="-codex-schema.json", delete=False, encoding="utf-8") as tmp_s:
        json.dump(DECISION_SCHEMA, tmp_s)
        schema_file = tmp_s.name

    try:
        CODEX_WORKSPACE.mkdir(parents=True, exist_ok=True)
        model = model or str(_load_runtime_control()["model"])
        reasoning_effort = reasoning_effort or str(_load_runtime_control()["reasoning_effort"])
        if reasoning_effort not in {"minimal", "low", "medium", "high"}:
            reasoning_effort = "low"
        model_escaped = model.replace("'", "''")
        reasoning_arg = f'model_reasoning_effort="{reasoning_effort}"'
        script = (
            f"$p = Get-Content -Raw -Path '{_ps_quote_path(prompt_file)}'\n"
            "$binArgs = @(" 
            "'exec','--ephemeral','--ignore-user-config','--ignore-rules',"
            "'--skip-git-repo-check','--sandbox','read-only',"
            f"'--cd','{_ps_quote_path(CODEX_WORKSPACE)}',"
            f"'--model','{model_escaped}','--config','{reasoning_arg}',"
            f"'--color','never','--output-schema','{_ps_quote_path(schema_file)}','-')\n"
            f"$p | & '{_ps_quote_path(binary)}' @binArgs\n"
            "exit $LASTEXITCODE\n"
        )
        return _run_powershell(script, timeout)
    finally:
        Path(prompt_file).unlink(missing_ok=True)
        Path(schema_file).unlink(missing_ok=True)


def _extract_json(text: str) -> dict[str, Any] | None:
    clean = (text or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.IGNORECASE | re.DOTALL).strip()
    try:
        payload = json.loads(clean)
        if isinstance(payload, dict):
            return payload
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text or ""):
        try:
            payload, _ = decoder.raw_decode((text or "")[match.start() :])
            if isinstance(payload, dict):
                return payload
        except Exception:
            continue
    return None


def _preview_text(text: str, limit: int = 800) -> str:
    clean = " ".join((text or "").replace("\x00", " ").split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def _write_auth_response(data: dict[str, Any]) -> None:
    resp_path = RESPONSES_DIR / "_auth.json"
    resp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _handle_auth_request(binary: str, req_file: Path) -> None:
    """Refresh Codex credentials when requested, using device authentication."""
    request_data: dict[str, Any] = {}
    try:
        request_data = json.loads(req_file.read_text(encoding="utf-8"))
        req_ts = float(request_data.get("requested_at", time.time()))
    except Exception:
        req_ts = time.time()
    force_reauth = request_data.get("force_reauth") is True

    _write_auth_response({"status": "running", "lines": [], "started_at": req_ts})
    timeout = int(os.environ.get("CODEX_CLI_AUTH_TIMEOUT_SECONDS", "300"))
    all_lines: list[str] = []

    _log("auth: checking Codex CLI login status")
    status_script = f"& '{_ps_quote_path(binary)}' login status\nexit $LASTEXITCODE\n"
    try:
        status_output, status_rc = _run_powershell(status_script, min(timeout, 30))
    except Exception as exc:
        status_output, status_rc = f"status check failed: {exc}", 1

    for line in status_output.splitlines():
        all_lines.append(line)
    _write_auth_response({"status": "running", "lines": all_lines, "started_at": req_ts})

    if status_rc == 0 and "logged in" in status_output.lower() and not force_reauth:
        _log("auth: Codex CLI is already authenticated")
        _write_auth_response(
            {
                "status": "done",
                "lines": all_lines,
                "exit_code": 0,
                "started_at": req_ts,
                "finished_at": time.time(),
            }
        )
        return

    if force_reauth and status_rc == 0 and "logged in" in status_output.lower():
        _log("auth: clearing the existing Codex CLI session before device authentication")
        logout_script = f"& '{_ps_quote_path(binary)}' logout\nexit $LASTEXITCODE\n"
        try:
            logout_output, logout_rc = _run_powershell(logout_script, min(timeout, 30))
        except Exception as exc:
            logout_output, logout_rc = f"logout failed: {exc}", 1
        all_lines.extend(logout_output.splitlines())
        _write_auth_response({"status": "running", "lines": all_lines, "started_at": req_ts})
        if logout_rc != 0:
            _log(f"auth: unable to clear the existing session rc={logout_rc}")
            _write_auth_response(
                {
                    "status": "error",
                    "lines": all_lines,
                    "exit_code": logout_rc,
                    "started_at": req_ts,
                    "finished_at": time.time(),
                    "error": "Could not clear the existing Codex CLI session.",
                }
            )
            return

    _log("auth: starting Codex device authentication")
    import tempfile

    script = f"& '{_ps_quote_path(binary)}' login --device-auth\nexit $LASTEXITCODE\n"
    with tempfile.NamedTemporaryFile(mode="w", suffix="-codex-auth.ps1", delete=False, encoding="utf-8") as tmp:
        tmp.write(script)
        script_file = tmp.name

    line_q: queue.Queue[dict[str, str]] = queue.Queue()
    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", script_file],
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
        )

        def _read(stream: Any, src: str) -> None:
            for raw in stream:
                line_q.put({"line": raw.rstrip("\r\n"), "src": src})

        threading.Thread(target=_read, args=(proc.stdout, "out"), daemon=True).start()
        threading.Thread(target=_read, args=(proc.stderr, "err"), daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            while True:
                try:
                    item = line_q.get_nowait()
                except queue.Empty:
                    break
                all_lines.append(item["line"])
                _write_auth_response({"status": "running", "lines": all_lines, "started_at": req_ts})
            if proc.poll() is not None:
                break
            time.sleep(0.5)

        if proc.poll() is None:
            _kill_tree(proc.pid)
            all_lines.append(f"Timed out after {timeout}s")
            exit_code: int | None = None
            status = "error"
        else:
            exit_code = proc.returncode
            status = "done" if exit_code == 0 else "error"
        _write_auth_response(
            {
                "status": status,
                "lines": all_lines,
                "exit_code": exit_code,
                "started_at": req_ts,
                "finished_at": time.time(),
            }
        )
        _log(f"auth: finished rc={exit_code}, lines={len(all_lines)}")
    except Exception as exc:
        _log(f"auth: error: {exc}")
        _write_auth_response(
            {
                "status": "error",
                "lines": all_lines + [str(exc)],
                "error": str(exc),
                "started_at": req_ts,
                "finished_at": time.time(),
            }
        )
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
        Path(script_file).unlink(missing_ok=True)


_processed_at: dict[str, float] = {}


def _claim_codex_budget(
    *,
    now: float | None = None,
    state_file: Path = BUDGET_FILE,
) -> tuple[bool, int]:
    """Atomically reserve one call from the application-wide rolling hourly budget."""
    current = float(now if now is not None else time.time())
    window_seconds = 3600
    try:
        max_calls = max(0, int(os.environ.get("CODEX_CLI_MAX_CALLS_PER_HOUR", "1")))
    except (TypeError, ValueError):
        max_calls = 1

    calls: list[float] = []
    try:
        if state_file.exists():
            raw_calls = json.loads(state_file.read_text(encoding="utf-8")).get("calls", [])
            calls = [float(item) for item in raw_calls if current - float(item) < window_seconds]
    except Exception:
        calls = []

    if max_calls <= 0:
        return False, window_seconds
    if len(calls) >= max_calls:
        retry_after = max(1, int(window_seconds - (current - min(calls))))
        return False, retry_after

    calls.append(current)
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temp_file = state_file.with_suffix(state_file.suffix + ".tmp")
    temp_file.write_text(
        json.dumps(
            {
                "calls": calls,
                "max_calls_per_hour": max_calls,
                "model": os.environ.get("CODEX_CLI_MODEL", "gpt-5.4-mini"),
                "updated_at": current,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temp_file.replace(state_file)
    return True, 0


def _request_sort_key(req_file: Path) -> tuple[int, float, str]:
    """Prefer invested positions before flat candidates when one global call is available."""
    try:
        data = json.loads(req_file.read_text(encoding="utf-8"))
        priority = int(data.get("priority", 0))
        requested_at = float(data.get("requested_at", 0))
        return -priority, -requested_at, req_file.stem
    except Exception:
        return 0, 0.0, req_file.stem


def _process_requests(binary: str) -> int:
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)

    processed = 0
    control = _load_runtime_control()
    auth_req = REQUESTS_DIR / "_auth.json"
    auth_resp = RESPONSES_DIR / "_auth.json"
    if auth_req.exists():
        raw = auth_req.read_text(encoding="utf-8").strip()
        if raw not in ("{}", ""):
            already_done = False
            try:
                req_ts = float(json.loads(raw).get("requested_at", 0))
                if auth_resp.exists():
                    resp = json.loads(auth_resp.read_text(encoding="utf-8"))
                    if resp.get("status") in ("done", "error") and float(resp.get("started_at", -1)) >= req_ts:
                        already_done = True
            except Exception:
                pass
            if not already_done:
                _handle_auth_request(binary, auth_req)
                return 1

    for req_file in sorted(REQUESTS_DIR.glob("*.json"), key=_request_sort_key):
        symbol = req_file.stem
        if symbol == "_auth":
            continue
        try:
            raw = req_file.read_text(encoding="utf-8").strip()
            if raw in ("{}", ""):
                continue
            req_data = json.loads(raw)
            requested_at = float(req_data.get("requested_at", 0))
            if time.time() - requested_at > 600:
                continue
            if _processed_at.get(symbol, 0) >= requested_at:
                continue
            if req_data.get("codex_eligible") is not True:
                _processed_at[symbol] = requested_at
                continue

            if control.get("enabled") is not True:
                response = {
                    "symbol": symbol,
                    "requested_at": requested_at,
                    "responded_at": time.time(),
                    "returncode": None,
                    "payload": None,
                    "status": "decision_disabled_by_owner",
                    "human_summary": "Codex CLI decisions are disabled by the owner control panel.",
                    "output_preview": "",
                    "error_preview": "",
                    "model": control.get("model"),
                }
                (RESPONSES_DIR / f"{symbol}.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
                _processed_at[symbol] = requested_at
                processed += 1
                continue

            budget_available, retry_after = _claim_codex_budget()
            if not budget_available:
                response = {
                    "symbol": symbol,
                    "requested_at": requested_at,
                    "responded_at": time.time(),
                    "returncode": None,
                    "payload": None,
                    "status": "decision_deferred_global_budget",
                    "human_summary": f"Codex hourly budget is reserved; retry in about {retry_after}s.",
                    "output_preview": "",
                    "error_preview": "",
                    "retry_after_seconds": retry_after,
                }
                (RESPONSES_DIR / f"{symbol}.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
                _processed_at[symbol] = requested_at
                processed += 1
                continue

            prompt_text = json.dumps(
                {
                    "task": "Select conservative bounded parameters; preserve all risk controls.",
                    "constraints": {
                        "target_pct": [0.003, 0.035],
                        "target_profit_usdt": [3, 200],
                        "stop_loss_pct": [0.005, 0.05],
                        "dca_levels": [1, 8],
                        "dca_reprice_seconds": [60, 600],
                        "reason_max_chars": 160,
                    },
                    "market": req_data.get("snapshot", {}),
                    "baseline": req_data.get("baseline", {}),
                },
                separators=(",", ":"),
            )

            _log(f"calling codex for {symbol} ...")
            timeout = int(os.environ.get("CODEX_CLI_DECISION_TIMEOUT_SECONDS", "60"))
            output, rc = _call_codex(binary, prompt_text, timeout, str(control.get("model")), str(control.get("reasoning_effort")))
            payload = _extract_json(output)
            output_preview = _preview_text(output)
            status = "decision_available" if payload and rc == 0 else f"decision_invalid_output:{rc}"
            if payload:
                human_summary = (
                    f"{str(payload.get('action', 'MONITOR')).upper()} "
                    f"confidence={payload.get('confidence', '-')} "
                    f"mode={payload.get('strategy_mode') or payload.get('regime') or '-'}: "
                    f"{payload.get('reason') or 'Codex returned a structured decision.'}"
                )
            else:
                human_summary = (
                    f"Codex exited with code {rc} but did not return strict JSON. "
                    "The bot kept its rule-based strategy parameters."
                )

            response = {
                "symbol": symbol,
                "requested_at": requested_at,
                "responded_at": time.time(),
                "model": control.get("model"),
                "returncode": rc,
                "payload": payload,
                "status": status,
                "human_summary": human_summary,
                "output_preview": output_preview,
                "error_preview": "" if payload else output_preview,
            }
            (RESPONSES_DIR / f"{symbol}.json").write_text(json.dumps(response, indent=2), encoding="utf-8")
            _processed_at[symbol] = requested_at
            processed += 1
            _log(f"done {symbol}: {response['status']}")
        except subprocess.TimeoutExpired:
            _log(f"timeout for {symbol}")
            timeout_value = int(os.environ.get("CODEX_CLI_DECISION_TIMEOUT_SECONDS", "60"))
            (RESPONSES_DIR / f"{symbol}.json").write_text(
                json.dumps(
                    {
                        "symbol": symbol,
                        "requested_at": requested_at,
                        "status": "decision_failed:TimeoutExpired",
                        "responded_at": time.time(),
                        "payload": None,
                        "human_summary": (
                            f"Codex CLI timed out after {timeout_value}s for {symbol}. "
                            "The bot kept rule-based strategy parameters."
                        ),
                        "output_preview": "",
                        "error_preview": "TimeoutExpired",
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
            _processed_at[symbol] = requested_at
        except Exception as exc:
            _log(f"error processing {symbol}: {exc}")

    return processed


def _install_task() -> None:
    python_exe = str(Path(sys.executable).resolve())
    script = str(Path(__file__).resolve())
    task_xml = f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>BitRucker Codex CLI strategy daemon (runs as current user)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions>
    <Exec>
      <Command>{python_exe}</Command>
      <Arguments>"{script}"</Arguments>
      <WorkingDirectory>{ROOT}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>"""

    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as tmp:
        tmp.write(task_xml)
        tmp_path = tmp.name

    result = subprocess.run(["schtasks", "/Create", "/F", "/TN", TASK_NAME, "/XML", tmp_path], capture_output=True, text=True)
    Path(tmp_path).unlink(missing_ok=True)
    if result.returncode == 0:
        print(f"Task '{TASK_NAME}' installed. Starting now ...")
        subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], check=False)
        print("Codex daemon is now running in the background.")
    else:
        print(f"ERROR installing task: {result.stdout} {result.stderr}")
        sys.exit(1)


def _acquire_lock() -> bool:
    global _LOCK_HANDLE

    try:
        import msvcrt

        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        handle = LOCK_FILE.open("a+b")
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        _LOCK_HANDLE = handle
        return True
    except OSError:
        try:
            handle.close()  # type: ignore[name-defined]
        except Exception:
            pass
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="BitRucker Codex strategy daemon")
    parser.add_argument("--install", action="store_true", help="Install as Windows scheduled task and exit")
    args = parser.parse_args()

    _load_web_config()
    if args.install:
        _install_task()
        return

    if not _acquire_lock():
        print("Another instance is already running. Exiting.")
        sys.exit(0)

    binary = _get_binary()
    if not binary:
        _log("ERROR: Codex CLI binary not found. Set CODEX_CLI_BINARY in web.config.")
        sys.exit(1)

    _log(f"started - binary={binary}")
    _log(f"requests dir: {REQUESTS_DIR}")
    _log(f"responses dir: {RESPONSES_DIR}")

    while True:
        try:
            count = _process_requests(binary)
            time.sleep(0 if count else 2)
        except KeyboardInterrupt:
            _log("stopped by user")
            break
        except Exception as exc:
            _log(f"unexpected error: {exc}")
            time.sleep(5)

    (ROOT / "runtime" / "codex-daemon.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
