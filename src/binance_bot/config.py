from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv


def _as_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_decimal(value: object, default: str) -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    return Decimal(str(value))


@dataclass(frozen=True)
class AssetRule:
    asset: str
    enabled: bool = True
    symbol: str | None = None
    profit_target_usd: Decimal | None = None
    profit_target_pct: Decimal | None = None
    reentry_quote_budget_usd: Decimal | None = None
    twap_duration_seconds: int | None = None
    twap_slices: int | None = None
    twap_interval_seconds: int | None = None
    start_cycle_when_flat: bool = False


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    dry_run: bool
    poll_interval_seconds: int
    profit_target_usd: Decimal
    profit_target_pct: Decimal
    stable_quote_assets: tuple[str, ...]
    exclude_assets: frozenset[str]
    default_reentry_quote_budget_usd: Decimal
    default_twap_duration_seconds: int
    algo_confirmation_grace_seconds: int
    default_twap_slices: int
    default_twap_interval_seconds: int
    notify_on_slice: bool
    api_key: str
    api_secret: str
    telegram_api_key: str | None
    telegram_chat_id: str | None
    notifications_enabled: bool
    dashboard_url: str | None
    state_file: Path
    assets: dict[str, AssetRule]
    bitget_api_key: str | None
    bitget_api_secret: str | None
    bitget_passphrase: str | None
    bitget_enabled: bool


def load_settings(root_dir: Path) -> Settings:
    load_dotenv(root_dir / ".env")
    config_path = root_dir / "strategy.json"
    data: dict[str, object] = {}
    if config_path.exists():
        data = json.loads(config_path.read_text(encoding="utf-8"))

    asset_rules: dict[str, AssetRule] = {}
    for asset, raw_rule in dict(data.get("assets", {})).items():
        rule = dict(raw_rule)
        asset_rules[asset.upper()] = AssetRule(
            asset=asset.upper(),
            enabled=_as_bool(rule.get("enabled"), True),
            symbol=str(rule.get("symbol")).upper() if rule.get("symbol") else None,
            profit_target_usd=_as_decimal(rule.get("profit_target_usd"), "0") if rule.get("profit_target_usd") is not None else None,
            profit_target_pct=_as_decimal(rule.get("profit_target_pct"), "0") if rule.get("profit_target_pct") is not None else None,
            reentry_quote_budget_usd=_as_decimal(rule.get("reentry_quote_budget_usd"), "0") if rule.get("reentry_quote_budget_usd") is not None else None,
            twap_duration_seconds=int(rule.get("twap_duration_seconds")) if rule.get("twap_duration_seconds") is not None else None,
            twap_slices=int(rule.get("twap_slices")) if rule.get("twap_slices") is not None else None,
            twap_interval_seconds=int(rule.get("twap_interval_seconds")) if rule.get("twap_interval_seconds") is not None else None,
            start_cycle_when_flat=_as_bool(rule.get("start_cycle_when_flat"), False),
        )

    api_key = os.getenv("BINANCE_API_KEY", "").strip()
    api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET must be set in .env")

    dashboard_url = os.getenv("DASHBOARD_URL", "").strip() or str(data.get("dashboard_url", "")).strip() or None

    default_twap_slices = max(1, int(data.get("default_twap_slices", 4)))
    default_twap_interval_seconds = max(1, int(data.get("default_twap_interval_seconds", 300)))
    default_twap_duration_seconds = int(data.get("default_twap_duration_seconds", max(300, default_twap_slices * default_twap_interval_seconds)))

    return Settings(
        root_dir=root_dir,
        dry_run=_as_bool(data.get("dry_run"), True),
        poll_interval_seconds=int(data.get("poll_interval_seconds", 30)),
        profit_target_usd=_as_decimal(data.get("profit_target_usd"), "1.00"),
        profit_target_pct=_as_decimal(data.get("profit_target_pct"), "0"),
        stable_quote_assets=tuple(str(item).upper() for item in list(data.get("stable_quote_assets", ["USDT", "FDUSD", "USDC", "BUSD"]))),
        exclude_assets=frozenset(str(item).upper() for item in list(data.get("exclude_assets", []))),
        default_reentry_quote_budget_usd=_as_decimal(data.get("default_reentry_quote_budget_usd"), "10.00"),
        default_twap_duration_seconds=max(300, default_twap_duration_seconds),
        algo_confirmation_grace_seconds=max(30, int(data.get("algo_confirmation_grace_seconds", 120))),
        default_twap_slices=default_twap_slices,
        default_twap_interval_seconds=default_twap_interval_seconds,
        notify_on_slice=_as_bool(data.get("notify_on_slice"), True),
        api_key=api_key,
        api_secret=api_secret,
        telegram_api_key=os.getenv("TELEGRAM_API_KEY", "").strip() or None,
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip() or None,
        notifications_enabled=_as_bool(os.getenv("NOTIFICATIONS_ENABLED"), True),
        dashboard_url=dashboard_url,
        state_file=root_dir / "runtime" / "state.json",
        assets=asset_rules,
        bitget_api_key=os.getenv("BITGET_AccessAPIKey", "").strip() or None,
        bitget_api_secret=os.getenv("BITGET_secretkey", "").strip() or None,
        bitget_passphrase=os.getenv("BITGET_PASSWORD", "").strip() or None,
        bitget_enabled=bool(
            os.getenv("BITGET_AccessAPIKey", "").strip()
            and os.getenv("BITGET_secretkey", "").strip()
            and os.getenv("BITGET_PASSWORD", "").strip()
        ),
    )
