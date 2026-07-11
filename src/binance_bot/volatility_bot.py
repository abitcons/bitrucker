"""
HIGH-GAIN Volatility-triggered bot with multi-signal entry & intelligent exit.
- Wilder's RSI (matches TradingView) + sentiment gating for precise entries
- Trailing stop-lock, stop-loss, overbought exit, and time-based exit
- Live Binance order placement via signed API
- Real-time dashboard with Telegram notifications on entry AND exit
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import shlex
import statistics
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Callable

from dotenv import load_dotenv

LOGGER = logging.getLogger(__name__)
BINANCE_BASE_URL = "https://api.binance.com"
BITGET_BASE_URL = "https://api.bitget.com"
BINANCE_FUTURES_BASE_URL = "https://fapi.binance.com"
# Primary exchange selection is persisted under runtime/primary_exchange.json.


def _load_web_config_env(start_dir: Path) -> None:
    for candidate_dir in [start_dir, *start_dir.parents]:
        config_path = candidate_dir / "web.config"
        if not config_path.exists():
            continue
        try:
            root = ET.parse(config_path).getroot()
            for item in root.findall("./appSettings/add"):
                key = item.attrib.get("key")
                value = item.attrib.get("value")
                if key and value is not None:
                    os.environ[key] = value
        except Exception as exc:
            LOGGER.warning("Unable to load appSettings from %s: %s", config_path, exc)
        return


@dataclass
class BotState:
    """Runtime state for the high-gain volatility bot"""
    status: str  # "idle", "waiting", "trading", "completed", "error"
    symbol: str = "PAXGUSDT"
    primary_exchange: str = "binance"
    current_volatility: float = 0.0
    current_price: float = 0.0
    is_volatile: bool = False
    volatility_threshold: float = 0.02  # 2% volatility threshold
    
    # Market state
    position_qty: Decimal = Decimal("0")
    position_free_qty: Decimal = Decimal("0")
    position_locked_qty: Decimal = Decimal("0")
    position_cost_basis: Decimal = Decimal("0")
    cost_basis_source: str = "none"
    entry_price: Decimal | None = None
    current_profit_usdt: Decimal = Decimal("0")
    target_profit_usdt: Decimal = Decimal("10")
    target_profit_suggested_usdt: Decimal = Decimal("10")
    target_profit_suggestion_reason: str = "Waiting for market data"
    target_profit_suggestion_source: str = "rules"
    target_profit_suggestion_updated_at: float = 0.0
    target_profit_auto_apply: bool = True
    target_profit_auto_apply_min_delta_usdt: Decimal = Decimal("0.50")
    target_profit_last_auto_applied_at: float = 0.0
    target_profit_auto_apply_note: str = "Auto target is enabled"
    target_sell_order_id: str | None = None
    target_sell_client_order_id: str | None = None
    target_sell_status: str = "idle"
    target_sell_price: Decimal = Decimal("0")
    target_sell_qty: Decimal = Decimal("0")
    target_sell_interval_seconds: int = 60
    target_sell_last_checked_at: float = 0.0
    stop_loss_order_id: str | None = None
    stop_loss_client_order_id: str | None = None
    stop_loss_status: str = "idle"
    stop_loss_price: Decimal = Decimal("0")
    stop_loss_qty: Decimal = Decimal("0")
    stop_loss_last_checked_at: float = 0.0
    
    # Market indicators
    current_rsi: float = 50.0
    rsi_threshold: float = 30.0  # RSI < 30 = oversold (entry signal)
    
    # Market sentiment (0-100: 0=fear, 50=neutral, 100=greed)
    market_sentiment: float = 50.0
    sentiment_name: str = "neutral"
    media_sentiment_score: float = 50.0
    media_sentiment_label: str = "neutral"
    media_sentiment_summary: str = "No media signal yet"
    media_news_headlines: str = ""
    liquidation_pressure_score: float = 0.0
    liquidation_bias: str = "neutral"
    liquidation_notional_usdt: float = 0.0
    liquidation_summary: str = "No liquidation signal yet"

    # Read-only trading advisor signal. This never places orders by itself.
    advisor_action: str = "MONITOR"
    advisor_confidence: float = 0.0
    advisor_summary: str = "Waiting for market data"
    advisor_source: str = "rules"
    advisor_updated_at: float = 0.0
    codex_cli_status: str = "not_checked"

    # Whole spot-account asset snapshot in USDT terms.
    total_asset_value_usdt: Decimal = Decimal("0")
    total_asset_count: int = 0
    total_asset_summary: str = "No asset snapshot yet"
    total_asset_updated_at: float = 0.0
    
    # Budget tracking
    budget_total: Decimal = Decimal("5000")
    budget_remaining: Decimal = Decimal("5000")

    # Risk controls
    equity_risk_per_trade_pct: Decimal = Decimal("0.01")
    risk_stop_move_pct: Decimal = Decimal("0.012")
    recommended_twap_quote_budget: Decimal = Decimal("0")
    last_risk_note: str = "Risk model waiting for equity snapshot"
    daily_drawdown_limit_pct: Decimal = Decimal("0.03")
    daily_equity_baseline_usdt: Decimal = Decimal("0")
    daily_drawdown_pct: float = 0.0
    daily_drawdown_triggered: bool = False
    daily_drawdown_day: str = ""
    
    # Timing
    started_at: float | None = None
    duration_hours: float = 12.0
    last_update: float = 0.0
    
    # Control
    user_requested_start: bool = False
    user_requested_stop: bool = False
    auto_restart: bool = False
    
    # Cycle tracking
    cycles_completed: int = 0
    
    # === HIGH-GAIN EDGE FIELDS ===
    # Stop-loss protection
    max_loss_usdt: Decimal = Decimal("50")
    
    # Position timing
    entry_time: float = 0.0
    max_position_hours: float = 4.0
    
    # Trailing profit stop (locks in gains)
    trailing_stop_usdt: Decimal = Decimal("5")
    peak_profit_usdt: Decimal = Decimal("0")
    
    # Sentiment gating threshold (only enter when fear/neutral)
    sentiment_entry_threshold: float = 55.0  # < 55 = fear or neutral

    # Native Spot TWAP swing-cycle settings
    strategy_mode: str = "auto"  # auto | spot | twap
    strategy_active: str = "twap"
    strategy_note: str = "Auto strategy: use spot entry for small budgets, switch to TWAP when budget is sufficient"
    twap_auto_switch_budget_usdt: Decimal = Decimal("1000")
    twap_duration_seconds: int = 60 * 60
    min_twap_quote_budget_usdt: Decimal = Decimal("1000")
    max_twap_quote_budget_usdt: Decimal = Decimal("1500")
    twap_rsi_start_threshold: float = 30.0
    active_twap_algo_id: str | None = None
    active_twap_client_algo_id: str | None = None
    active_twap_status: str = "idle"
    active_twap_started_at: float = 0.0
    active_twap_quote_budget: Decimal = Decimal("0")
    active_twap_executed_quote: Decimal = Decimal("0")
    last_twap_fill_at: float = 0.0
    dca_last_repriced_at: float = 0.0
    dca_order_count: int = 0
    adaptive_dca_levels: int = 3
    adaptive_dca_reprice_interval_seconds: int = 180
    adaptive_dca_min_step_pct: Decimal = Decimal("0.003")
    adaptive_dca_max_step_pct: Decimal = Decimal("0.015")
    adaptive_max_entry_balance_fraction: Decimal = Decimal("0.20")
    adaptive_stop_loss_pct: Decimal = Decimal("0.020")   # 2% of position by default
    adaptive_strategy_mode: str = "swing"                 # swing | momentum | defensive | mean_reversion
    strategy_analysis_updated_at: float = 0.0
    strategy_analysis_source: str = "rules"
    strategy_analysis_summary: str = "Waiting for strategy analysis"
    last_exit_reason: str = ""
    manual_entry_override_until: float = 0.0


class VolatilityTwapBot:
    symbol = "PAXGUSDT"
    quote_asset = "USDT"
    quote_asset_suffixes = ("USDT", "FDUSD", "USDC", "BUSD")
    _shared_exchange_symbols_cache: dict[str, tuple[float, set[str]]] = {}
    _shared_exchange_instruments_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}
    _shared_exchange_symbols_lock = threading.RLock()
    supported_symbols = (
        "PAXGUSDT", "BTCUSDT", "ETHUSDT", "UNIUSDT", "EPICUSDT", "BNBUSDT", "SOLUSDT",
        "LINKUSDT", "LTCUSDT",
        "BTCFDUSD", "ETHFDUSD", "BNBFDUSD", "SOLFDUSD", "LINKFDUSD", "LTCFDUSD",
    )

    def __init__(
        self,
        root_dir: Path,
        api_key: str,
        api_secret: str,
        dry_run: bool = True,
        *,
        primary_exchange: str | None = None,
        exchange_credentials: dict[str, dict[str, str]] | None = None,
    ):
        _load_web_config_env(root_dir)
        for candidate_dir in [root_dir, *root_dir.parents]:
            env_path = candidate_dir / ".env"
            if env_path.exists():
                load_dotenv(env_path, override=False)
                break
        self.root_dir = root_dir
        credentials_are_explicit = exchange_credentials is not None
        explicit_credentials = exchange_credentials or {}
        binance_credentials = explicit_credentials.get("binance") or {}
        bitget_credentials = explicit_credentials.get("bitget") or {}
        self.api_key = str(binance_credentials.get("api_key") or ("" if credentials_are_explicit else api_key))
        self.api_secret = str(binance_credentials.get("api_secret") or ("" if credentials_are_explicit else api_secret))
        self.dry_run = dry_run
        self.state_file = root_dir / "runtime" / "bot_state.json"
        shared_runtime_dir = root_dir.parent if root_dir.parent.name == "runtime" else root_dir / "runtime"
        self.primary_exchange_file = shared_runtime_dir / "primary_exchange.json"
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state = self._load_state()
        configured_exchange = str(primary_exchange or os.getenv("PRIMARY_EXCHANGE", "binance")).strip().lower()
        if configured_exchange not in {"binance", "bitget"}:
            configured_exchange = "binance"
        try:
            shared_exchange = json.loads(self.primary_exchange_file.read_text(encoding="utf-8")).get("primary_exchange")
            if str(shared_exchange).lower() in {"binance", "bitget"}:
                configured_exchange = str(shared_exchange).lower()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        try:
            persisted_state = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        except Exception:
            persisted_state = {}
        if "primary_exchange" not in persisted_state:
            self.state.primary_exchange = configured_exchange
        self.primary_exchange = self.state.primary_exchange if self.state.primary_exchange in {"binance", "bitget"} else configured_exchange
        self.state.primary_exchange = self.primary_exchange
        self.bitget_api_key = str(
            bitget_credentials.get("api_key")
            or ("" if credentials_are_explicit else os.getenv("BITGET_AccessAPIKey", ""))
        ).strip()
        self.bitget_api_secret = str(
            bitget_credentials.get("api_secret")
            or ("" if credentials_are_explicit else os.getenv("BITGET_secretkey", ""))
        ).strip()
        self.bitget_passphrase = str(
            bitget_credentials.get("passphrase")
            or ("" if credentials_are_explicit else os.getenv("BITGET_PASSWORD", ""))
        ).strip()
        self.state.twap_rsi_start_threshold = 30.0
        self.symbol = self._normalize_symbol(self.state.symbol)
        self.state.symbol = self.symbol
        self.running = False
        self.thread: threading.Thread | None = None
        self.entry_guard: Callable[[str], tuple[bool, str]] | None = None
        self.lock = threading.RLock()
        self._quantity_step_size_cache: Decimal | None = None
        self._price_tick_size_cache: Decimal | None = None
        self._candidate_cache: dict[str, Any] | None = None
        self._candidate_cache_key: tuple[str, ...] = ()
        self._candidate_cache_at = 0.0
        self._exchange_symbols_cache: tuple[float, set[str]] | None = None
        self._exchange_instruments_cache: tuple[float, dict[str, dict[str, Any]]] | None = None
        self._bitget_account_mode_cache: tuple[float, str, str] | None = None
        self._volume_symbols_cache: tuple[float, list[str]] | None = None
        self._book_ticker_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._depth_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._external_liquidity_cache: dict[str, tuple[float, tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]]] = {}
        self._liquidation_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._fee_cache: tuple[float, dict[str, float]] | None = None
        self._media_cache: tuple[float, str, str] | None = None
        self._media_headlines_cache = ""
        self._media_cache_at = 0.0
        self._codex_cli_status_cache: str | None = None
        self._codex_cli_status_cache_at = 0.0
        self.min_quote_volume_24h_usdt = float(os.getenv("BOT_MIN_QUOTE_VOLUME_24H_USDT", "10000000"))
        self.max_spread_bps = float(os.getenv("BOT_MAX_SPREAD_BPS", "25"))
        self.min_depth_notional_usdt = float(os.getenv("BOT_MIN_DEPTH_NOTIONAL_USDT", "50000"))
        self.max_fee_bps = float(os.getenv("BOT_MAX_FEE_BPS", "15"))
        self.default_fee_bps = float(os.getenv("BOT_DEFAULT_FEE_BPS", "10"))
        self.max_entry_balance_fraction = max(Decimal("0.01"), min(Decimal("1"), Decimal(os.getenv("BOT_MAX_ENTRY_BALANCE_FRACTION", "0.20"))))
        self.max_order_loss_usdt = max(Decimal("0.01"), Decimal(os.getenv("BOT_MAX_ORDER_LOSS_USDT", "15.00")))
        self.min_stop_loss_usdt = max(Decimal("0"), Decimal(os.getenv("BOT_MIN_STOP_LOSS_USDT", "0.25")))
        self.dust_position_cost_usdt = max(Decimal("0"), Decimal(os.getenv("BOT_DUST_POSITION_COST_USDT", "5.00")))
        self.strict_entry_rsi = max(1.0, min(100.0, float(os.getenv("BOT_STRICT_ENTRY_RSI_MAX", "30"))))
        self.dca_levels = max(1, min(8, int(os.getenv("BOT_DCA_LEVELS", "3"))))
        self.dca_reprice_interval_seconds = max(30, int(os.getenv("BOT_DCA_REPRICE_INTERVAL_SECONDS", "180")))
        self.dca_min_step_pct = max(Decimal("0.001"), Decimal(os.getenv("BOT_DCA_MIN_STEP_PCT", "0.003")))
        self.dca_max_step_pct = max(self.dca_min_step_pct, Decimal(os.getenv("BOT_DCA_MAX_STEP_PCT", "0.015")))
        self.strategy_analysis_interval_seconds = max(3600, int(os.getenv("CODEX_STRATEGY_ANALYSIS_INTERVAL_SECONDS", "3600")))
        self.state.twap_duration_seconds = max(300, int(os.getenv("BOT_TWAP_DURATION_SECONDS", str(self.state.twap_duration_seconds or 3600))))
        if self.state.twap_duration_seconds > 3600 and os.getenv("BOT_FORCE_ONE_HOUR_TWAP", "true").lower() in {"1", "true", "yes", "on"}:
            self.state.twap_duration_seconds = 3600
        self._normalize_adaptive_strategy_parameters()
        self.waiting_poll_seconds = max(5, int(os.getenv("BOT_WAITING_POLL_SECONDS", "30")))
        self.trading_poll_seconds = max(2, int(os.getenv("BOT_TRADING_POLL_SECONDS", "5")))
        self.codex_decision_enabled = (
            False
            if credentials_are_explicit
            else os.getenv("CODEX_CLI_DECISION_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
        )
        self.codex_decision_command = os.getenv("CODEX_CLI_DECISION_COMMAND", "").strip()
        self.codex_active_only = os.getenv("CODEX_CLI_ACTIVE_ONLY", "true").lower() in {"1", "true", "yes", "on"}
        self.codex_direct_fallback_enabled = os.getenv("CODEX_CLI_DIRECT_FALLBACK_ENABLED", "false").lower() in {"1", "true", "yes", "on"}
        self.codex_daemon_poll_seconds = max(1, min(10, int(os.getenv("CODEX_CLI_DAEMON_POLL_SECONDS", "5"))))
        try:
            self.codex_decision_timeout_seconds = max(3, min(30, int(os.getenv("CODEX_CLI_DECISION_TIMEOUT_SECONDS", "12"))))
        except Exception:
            self.codex_decision_timeout_seconds = 12
        
        # Telegram notification settings
        self.telegram_api_key = "" if credentials_are_explicit else os.getenv("TELEGRAM_API_KEY", "")
        self.telegram_chat_id = "" if credentials_are_explicit else os.getenv("TELEGRAM_CHAT_ID", "")
        self.notifications_enabled = (
            False
            if credentials_are_explicit
            else os.getenv("NOTIFICATIONS_ENABLED", "true").lower() == "true"
        )

    def _normalize_symbol(self, symbol: str | None) -> str:
        normalized = (symbol or self.symbol).upper().replace("/", "").strip()
        return normalized if normalized else self.symbol

    def _quote_asset_for_symbol(self, symbol: str | None = None) -> str:
        candidate = (symbol or self.symbol or "").upper().strip()
        for suffix in self.quote_asset_suffixes:
            if candidate.endswith(suffix):
                return suffix
        return self.quote_asset

    def _base_asset(self) -> str:
        quote_asset = self._quote_asset_for_symbol(self.symbol)
        if self.symbol.endswith(quote_asset):
            return self.symbol[:-len(quote_asset)]
        return self.symbol

    def resolve_exchange_symbol(self, symbol: str, force: bool = False) -> str:
        """Resolve a requested pair to a tradable pair on the selected exchange.

        The requested quote is preferred when it exists. If it does not, keep
        the same base asset and use the first supported stablecoin quote,
        preferring USDT. This prevents an exchange change from leaking an
        unsupported symbol into account, order, or market-data requests.
        """
        normalized = self._normalize_symbol(symbol)
        supported = self.get_exchange_supported_symbols(force=force)
        exchange_name = self.primary_exchange.upper()
        if not supported:
            raise RuntimeError(f"Unable to load tradable spot pairs from {exchange_name}")
        if normalized in supported:
            return normalized

        # Bitget exposes tokenized equities with an r-prefixed base coin (for
        # example NVDA/NVDAUSDT maps to rNVDAUSDT). Accept familiar stock ticker
        # input while keeping the exchange's canonical symbol internally.
        if self.primary_exchange == "bitget":
            for exchange_symbol, metadata in self.get_exchange_instruments(force=force).items():
                if str(metadata.get("instrument_type") or "").lower() != "stock":
                    continue
                base_coin = str(metadata.get("baseCoin") or "")
                underlying = base_coin[1:] if base_coin[:1].lower() == "r" else base_coin
                quote_coin = str(metadata.get("quoteCoin") or self.quote_asset).upper()
                if normalized in {underlying.upper(), f"{underlying}{quote_coin}".upper()}:
                    return exchange_symbol

        requested_quote = self._quote_asset_for_symbol(normalized)
        if not normalized.endswith(requested_quote):
            raise ValueError(f"Unsupported pair {normalized} on {exchange_name}")
        base_asset = normalized[:-len(requested_quote)]
        preferred_quotes = list(dict.fromkeys((requested_quote, "USDT", *self.quote_asset_suffixes)))
        for quote_asset in preferred_quotes:
            candidate = f"{base_asset}{quote_asset}"
            if candidate in supported:
                return candidate
        raise ValueError(
            f"Unsupported pair {normalized} on {exchange_name}; "
            f"no supported stablecoin spot pair was found for {base_asset}"
        )

    def get_exchange_instruments(self, force: bool = False) -> dict[str, dict[str, Any]]:
        now = time.time()
        if not force and self._exchange_instruments_cache and now - self._exchange_instruments_cache[0] < 3600:
            return {key: dict(value) for key, value in self._exchange_instruments_cache[1].items()}
        exchange_key = self.primary_exchange.lower()
        if not force:
            with self._shared_exchange_symbols_lock:
                shared = self._shared_exchange_instruments_cache.get(exchange_key)
            if shared and now - shared[0] < 3600:
                instruments = {key: dict(value) for key, value in shared[1].items()}
                self._exchange_instruments_cache = (shared[0], instruments)
                self._exchange_symbols_cache = (shared[0], set(instruments))
                return instruments
        instruments: dict[str, dict[str, Any]] = {}
        try:
            if self.primary_exchange == "bitget":
                payload = self._get_bitget_public_json("/api/v3/market/instruments?category=SPOT", 10)
                for item in payload.get("data") or []:
                    api_symbol = str(item.get("symbol") or "")
                    symbol = api_symbol.upper()
                    quote_asset = str(item.get("quoteCoin") or "").upper()
                    base_asset = str(item.get("baseCoin") or "")
                    if not symbol or str(item.get("status") or "").lower() != "online":
                        continue
                    if quote_asset not in self.quote_asset_suffixes or base_asset.upper() in self.quote_asset_suffixes:
                        continue
                    metadata = dict(item)
                    metadata["symbol"] = symbol
                    metadata["api_symbol"] = api_symbol or symbol
                    metadata["instrument_type"] = str(
                        item.get("symbolType")
                        or ("stock" if str(item.get("isReality") or "").lower() == "yes" else "crypto")
                    ).lower()
                    metadata["is_reality"] = str(item.get("isReality") or "no").lower() == "yes"
                    instruments[symbol] = metadata
            else:
                url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo"
                request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
                with urllib.request.urlopen(request, timeout=10) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                for item in payload.get("symbols") or []:
                    symbol = str(item.get("symbol") or "").upper()
                    if not symbol or item.get("status") != "TRADING":
                        continue
                    if not bool(item.get("isSpotTradingAllowed", True)):
                        continue
                    quote_asset = str(item.get("quoteAsset") or "").upper()
                    base_asset = str(item.get("baseAsset") or "").upper()
                    if quote_asset not in self.quote_asset_suffixes or base_asset in self.quote_asset_suffixes:
                        continue
                    metadata = dict(item)
                    metadata["symbol"] = symbol
                    metadata["baseCoin"] = item.get("baseAsset")
                    metadata["quoteCoin"] = item.get("quoteAsset")
                    metadata["instrument_type"] = "crypto"
                    metadata["is_reality"] = False
                    instruments[symbol] = metadata
        except Exception as exc:
            if self._exchange_instruments_cache:
                LOGGER.warning(
                    "Unable to refresh %s exchange instruments; using cached catalog: %s",
                    self.primary_exchange.upper(),
                    exc,
                )
                return {key: dict(value) for key, value in self._exchange_instruments_cache[1].items()}
            with self._shared_exchange_symbols_lock:
                shared = self._shared_exchange_instruments_cache.get(exchange_key)
            if shared:
                LOGGER.warning(
                    "Unable to refresh %s exchange instruments; using shared cached catalog: %s",
                    self.primary_exchange.upper(),
                    exc,
                )
                instruments = {key: dict(value) for key, value in shared[1].items()}
                self._exchange_instruments_cache = (shared[0], instruments)
                self._exchange_symbols_cache = (shared[0], set(instruments))
                return instruments
            LOGGER.warning("Unable to load %s exchange instruments: %s", self.primary_exchange.upper(), exc)
            return {}
        self._exchange_instruments_cache = (now, instruments)
        self._exchange_symbols_cache = (now, set(instruments))
        with self._shared_exchange_symbols_lock:
            self._shared_exchange_instruments_cache[exchange_key] = (
                now,
                {key: dict(value) for key, value in instruments.items()},
            )
            self._shared_exchange_symbols_cache[exchange_key] = (now, set(instruments))
        return {key: dict(value) for key, value in instruments.items()}

    def get_exchange_supported_symbols(self, force: bool = False) -> set[str]:
        return set(self.get_exchange_instruments(force=force))

    def get_instrument_metadata(self, symbol: str | None = None) -> dict[str, Any]:
        normalized = self._normalize_symbol(symbol)
        now = time.time()
        if self._exchange_instruments_cache and now - self._exchange_instruments_cache[0] < 3600:
            metadata = dict(self._exchange_instruments_cache[1].get(normalized, {}))
        else:
            metadata = dict(self.get_exchange_instruments().get(normalized, {}))
        if metadata or self.primary_exchange != "bitget":
            return metadata
        # Fail closed for r-prefixed Reality symbols if the UTA catalog has a
        # transient outage. The Classic public symbol endpoint still identifies
        # these via a lowercase-r base coin.
        try:
            item = self._get_bitget_symbol_info(normalized)
        except Exception:
            return {}
        base_coin = str(item.get("baseCoin") or "")
        is_reality = base_coin.startswith("r") and len(base_coin) > 1
        fallback = dict(item)
        fallback["symbol"] = normalized
        fallback["api_symbol"] = str(item.get("symbol") or normalized)
        fallback["instrument_type"] = "stock" if is_reality else "crypto"
        fallback["is_reality"] = is_reality
        return fallback

    def get_instrument_type(self, symbol: str | None = None) -> str:
        metadata = self.get_instrument_metadata(symbol)
        return str(metadata.get("instrument_type") or metadata.get("symbolType") or "crypto").lower()

    def is_stock_instrument(self, symbol: str | None = None) -> bool:
        metadata = self.get_instrument_metadata(symbol)
        return bool(metadata.get("is_reality")) or self.get_instrument_type(symbol) == "stock"

    def _stock_underlying_ticker(self, symbol: str | None = None) -> str:
        metadata = self.get_instrument_metadata(symbol)
        base_coin = str(metadata.get("baseCoin") or "")
        if base_coin.startswith("r") and len(base_coin) > 1:
            return base_coin[1:].upper()
        base_asset = self._base_asset()
        return base_asset[1:] if base_asset.startswith("R") and len(base_asset) > 1 else base_asset

    def get_discovery_symbols(self, limit: int = 40, force: bool = False) -> list[str]:
        now = time.time()
        safe_limit = max(5, min(100, int(limit)))
        if not force and self._volume_symbols_cache and now - self._volume_symbols_cache[0] < 300:
            return list(self._volume_symbols_cache[1][:safe_limit])
        allowed = self.get_exchange_supported_symbols(force=force)
        ranked: list[tuple[float, str]] = []
        try:
            if self.primary_exchange == "bitget":
                payload = self._get_bitget_public_json("/api/v2/spot/market/tickers?productType=SPOT", 10)
                for item in payload.get("data") or []:
                    symbol = str(item.get("symbol") or "").upper()
                    if symbol not in allowed:
                        continue
                    quote_volume = float(Decimal(str(item.get("quoteVolume") or item.get("usdtVolume") or "0")))
                    if quote_volume >= self.min_quote_volume_24h_usdt:
                        ranked.append((quote_volume, symbol))
                ranked.sort(reverse=True)
                configured_fallbacks = [symbol for symbol in self.supported_symbols if symbol in allowed]
                ranked_symbols = [symbol for _, symbol in ranked]
                stock_symbols = [symbol for symbol in ranked_symbols if self.is_stock_instrument(symbol)]
                stock_symbol_set = set(stock_symbols)
                other_symbols = [symbol for symbol in ranked_symbols if symbol not in stock_symbol_set]
                # Reality stocks dominate Bitget's volume table on some days. Keep
                # both asset classes visible instead of allowing either one to
                # crowd the other out of the discovery limit.
                balanced: list[str] = []
                for index in range(max(len(other_symbols), len(stock_symbols))):
                    if index < len(other_symbols):
                        balanced.append(other_symbols[index])
                    if index < len(stock_symbols):
                        balanced.append(stock_symbols[index])
                symbols = list(dict.fromkeys(balanced + configured_fallbacks))
                self._volume_symbols_cache = (now, symbols)
                return symbols[:safe_limit]
            url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
            request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            for item in payload if isinstance(payload, list) else []:
                symbol = str(item.get("symbol") or "").upper()
                if symbol not in allowed:
                    continue
                quote_volume = float(Decimal(str(item.get("quoteVolume", "0") or "0")))
                if quote_volume >= self.min_quote_volume_24h_usdt:
                    ranked.append((quote_volume, symbol))
        except Exception as exc:
            LOGGER.warning("Unable to load discovery symbols: %s", exc)
        ranked.sort(reverse=True)
        configured_fallbacks = [symbol for symbol in self.supported_symbols if symbol in allowed]
        symbols = list(dict.fromkeys([symbol for _, symbol in ranked] + configured_fallbacks))
        self._volume_symbols_cache = (now, symbols)
        return symbols[:safe_limit]

    def _can_switch_symbol_locked(self) -> bool:
        if self.dry_run:
            return True
        return self.state.status != "trading" and self.state.position_qty <= 0 and not self.state.active_twap_algo_id

    def _get_quantity_step_size(self) -> Decimal:
        if self._quantity_step_size_cache is not None:
            return self._quantity_step_size_cache
        fallback = Decimal("0.000001")
        try:
            if self.primary_exchange == "bitget":
                item = self._get_bitget_symbol_info(self.symbol)
                step_size = Decimal("1") / (Decimal("10") ** int(item.get("quantityPrecision", 6)))
                self._quantity_step_size_cache = step_size
                return step_size
            query = urllib.parse.urlencode({"symbol": self.symbol})
            url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo?{query}"
            request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            symbols = payload.get("symbols") or []
            filters = symbols[0].get("filters", []) if symbols else []
            for filter_data in filters:
                if filter_data.get("filterType") == "LOT_SIZE":
                    step_size = Decimal(str(filter_data.get("stepSize", "0")))
                    if step_size > 0:
                        self._quantity_step_size_cache = step_size
                        return step_size
        except Exception as exc:
            LOGGER.warning("Unable to fetch %s quantity step size: %s", self.symbol, exc)
        self._quantity_step_size_cache = fallback
        return fallback

    def _get_price_tick_size(self) -> Decimal:
        if self._price_tick_size_cache is not None:
            return self._price_tick_size_cache
        fallback = Decimal("0.01")
        try:
            if self.primary_exchange == "bitget":
                item = self._get_bitget_symbol_info(self.symbol)
                tick_size = Decimal("1") / (Decimal("10") ** int(item.get("pricePrecision", 2)))
                self._price_tick_size_cache = tick_size
                return tick_size
            query = urllib.parse.urlencode({"symbol": self.symbol})
            url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo?{query}"
            request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            symbols = payload.get("symbols") or []
            filters = symbols[0].get("filters", []) if symbols else []
            for filter_data in filters:
                if filter_data.get("filterType") == "PRICE_FILTER":
                    tick_size = Decimal(str(filter_data.get("tickSize", "0")))
                    if tick_size > 0:
                        self._price_tick_size_cache = tick_size
                        return tick_size
        except Exception as exc:
            LOGGER.warning("Unable to fetch %s price tick size: %s", self.symbol, exc)
        self._price_tick_size_cache = fallback
        return fallback

    def _round_price_up(self, price: Decimal) -> Decimal:
        tick = self._get_price_tick_size()
        ticks = (price / tick).to_integral_value(rounding=ROUND_UP)
        return ticks * tick

    def _round_price_down(self, price: Decimal) -> Decimal:
        tick = self._get_price_tick_size()
        ticks = (price / tick).to_integral_value(rounding=ROUND_DOWN)
        return ticks * tick

    def get_pair_candidates(self, force: bool = False, symbols: list[str] | None = None, max_seconds: float | None = None) -> dict[str, Any]:
        now = time.time()
        allowed_symbols = self.get_exchange_supported_symbols()
        source_symbols = symbols if symbols is not None else self.get_discovery_symbols()
        candidate_symbols = [
            self._normalize_symbol(symbol)
            for symbol in source_symbols
            if self._normalize_symbol(symbol) in allowed_symbols
        ]
        candidate_symbols = list(dict.fromkeys(candidate_symbols))
        cache_key = tuple(candidate_symbols)
        if not force and self._candidate_cache and self._candidate_cache_key == cache_key and now - self._candidate_cache_at < 120:
            return self._candidate_cache

        candidate_http_timeout = max(0.75, min(5.0, float(os.getenv("BOT_CANDIDATE_HTTP_TIMEOUT_SECONDS", "2.0"))))
        scan_budget_seconds = max_seconds if max_seconds is not None else float(os.getenv("BOT_CANDIDATE_SCAN_MAX_SECONDS", "8.0"))
        scan_budget_seconds = max(2.0, min(20.0, scan_budget_seconds))
        fee_map = self._fee_cache[1] if self._fee_cache and now - self._fee_cache[0] < 600 else {}
        candidates = []

        def rank_symbol(symbol: str) -> dict[str, Any] | None:
            try:
                instrument_type = self.get_instrument_type(symbol)
                volatility, rsi = self._get_market_data(symbol, timeout_seconds=candidate_http_timeout)
                ticker = self._get_24h_ticker(symbol, timeout_seconds=candidate_http_timeout)
                book_ticker = self._get_book_ticker(symbol, timeout_seconds=candidate_http_timeout)
                depth = self._get_orderbook_depth(symbol, timeout_seconds=candidate_http_timeout)
                liquidity_source = self.primary_exchange.title()
                liquidation_signal = self._get_liquidation_signal(symbol, timeout_seconds=candidate_http_timeout)
                price = Decimal(str(ticker.get("lastPrice") or ticker.get("weightedAvgPrice") or "0"))
                fee_bps = fee_map.get(symbol, self.default_fee_bps)
                score, reason, liquidity_ok, spread_bps, depth_notional, fee_ok = self._score_pair_candidate(
                    rsi,
                    volatility,
                    ticker,
                    book_ticker,
                    depth,
                    fee_bps,
                    liquidation_signal,
                )
                if not liquidity_ok:
                    try:
                        fallback_ticker, fallback_book, fallback_depth, fallback_source = self._get_external_liquidity_snapshot(symbol, timeout_seconds=candidate_http_timeout)
                        fallback_score, fallback_reason, fallback_liquidity_ok, fallback_spread_bps, fallback_depth_notional, fallback_fee_ok = self._score_pair_candidate(
                            rsi,
                            volatility,
                            fallback_ticker,
                            fallback_book,
                            fallback_depth,
                            fee_bps,
                            liquidation_signal,
                        )
                        if fallback_score > score or fallback_liquidity_ok:
                            ticker = fallback_ticker
                            book_ticker = fallback_book
                            depth = fallback_depth
                            price = Decimal(str(ticker.get("lastPrice") or ticker.get("weightedAvgPrice") or price or "0"))
                            score = fallback_score
                            reason = f"{fallback_reason}. Liquidity source: {fallback_source}; Binance warning: {reason}"
                            liquidity_ok = fallback_liquidity_ok
                            spread_bps = fallback_spread_bps
                            depth_notional = fallback_depth_notional
                            fee_ok = fallback_fee_ok
                            liquidity_source = fallback_source
                    except Exception as fallback_exc:
                        LOGGER.warning("External liquidity fallback unavailable for %s: %s", symbol, fallback_exc)
                return {
                    "symbol": symbol,
                    "instrument_type": instrument_type,
                    "is_stock": self.is_stock_instrument(symbol),
                    "base_asset": symbol[:-len(self._quote_asset_for_symbol(symbol))] if symbol.endswith(self._quote_asset_for_symbol(symbol)) else symbol,
                    "price": float(price),
                    "rsi": round(rsi, 2),
                    "volatility": round(volatility * 100, 3),
                    "price_change_24h": round(float(ticker.get("priceChangePercent", 0.0)), 2),
                    "quote_volume": float(Decimal(str(ticker.get("quoteVolume", "0")))),
                    "liquidity_ok": liquidity_ok,
                    "fee_ok": fee_ok,
                    "fee_bps": round(fee_bps, 3),
                    "spread_bps": round(spread_bps, 3),
                    "depth_notional_usdt": round(depth_notional, 2),
                    "liquidity_source": liquidity_source,
                    "liquidation_score": liquidation_signal.get("score", 0.0),
                    "liquidation_bias": liquidation_signal.get("bias", "neutral"),
                    "liquidation_notional_usdt": liquidation_signal.get("notional_usdt", 0.0),
                    "liquidation_source": liquidation_signal.get("source", "unavailable"),
                    "liquidation_summary": liquidation_signal.get("summary", "No liquidation signal"),
                    "score": score,
                    "reason": reason,
                }
            except Exception as exc:
                LOGGER.warning("Unable to rank %s: %s", symbol, exc)
                return None

        if candidate_symbols:
            max_workers = min(6, len(candidate_symbols))
            executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="candidate-rank")
            futures = {executor.submit(rank_symbol, symbol): symbol for symbol in candidate_symbols}
            try:
                for future in as_completed(futures, timeout=scan_budget_seconds):
                    candidate = future.result()
                    if candidate:
                        candidates.append(candidate)
            except FuturesTimeoutError:
                unfinished = [symbol for future, symbol in futures.items() if not future.done()]
                LOGGER.warning("Candidate ranking timed out after %.1fs; unfinished symbols: %s", scan_budget_seconds, ", ".join(unfinished))
            finally:
                for future in futures:
                    future.cancel()
                executor.shutdown(wait=False, cancel_futures=True)

        if not candidates and self._candidate_cache and self._candidate_cache_key == cache_key:
            return {**self._candidate_cache, "stale": True, "stale_reason": "Candidate refresh timed out; showing last ranking"}

        candidates.sort(key=lambda item: (not (item.get("liquidity_ok") and item.get("fee_ok")), -item["score"], item.get("fee_bps", 9999.0)))
        liquidity_candidates = [item for item in candidates if item.get("liquidity_ok") and item.get("fee_ok")]
        payload = {
            "generated_at": now,
            "selected_symbol": self.symbol,
            "best": liquidity_candidates[0] if liquidity_candidates else (candidates[0] if candidates else None),
            "candidates": candidates,
        }
        self._candidate_cache = payload
        self._candidate_cache_key = cache_key
        self._candidate_cache_at = now
        return payload

    def _score_pair_candidate(
        self,
        rsi: float,
        volatility: float,
        ticker: dict[str, Any],
        book_ticker: dict[str, Any],
        depth: dict[str, Any],
        fee_bps: float,
        liquidation_signal: dict[str, Any] | None = None,
    ) -> tuple[float, str, bool, float, float, bool]:
        quote_volume = float(Decimal(str(ticker.get("quoteVolume", "0"))))
        change_24h = float(ticker.get("priceChangePercent", 0.0))
        bid = float(Decimal(str(book_ticker.get("bidPrice", "0") or "0")))
        ask = float(Decimal(str(book_ticker.get("askPrice", "0") or "0")))
        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        spread_bps = ((ask - bid) / mid * 10000.0) if mid > 0 and ask >= bid else 9999.0
        bids = depth.get("bids") or []
        asks = depth.get("asks") or []
        bid_depth_notional = sum(float(Decimal(str(level[0])) * Decimal(str(level[1]))) for level in bids[:20] if len(level) >= 2)
        ask_depth_notional = sum(float(Decimal(str(level[0])) * Decimal(str(level[1]))) for level in asks[:20] if len(level) >= 2)
        depth_notional = min(bid_depth_notional, ask_depth_notional)

        liquidity_fail_reasons = []
        if quote_volume < self.min_quote_volume_24h_usdt:
            liquidity_fail_reasons.append(f"24h vol ${quote_volume/1_000_000:.2f}M < ${self.min_quote_volume_24h_usdt/1_000_000:.2f}M")
        if spread_bps > self.max_spread_bps:
            liquidity_fail_reasons.append(f"spread {spread_bps:.2f} bps > {self.max_spread_bps:.2f}")
        if depth_notional < self.min_depth_notional_usdt:
            liquidity_fail_reasons.append(f"depth20 ${depth_notional:,.0f} < ${self.min_depth_notional_usdt:,.0f}")
        liquidity_ok = not liquidity_fail_reasons
        fee_ok = fee_bps <= self.max_fee_bps
        if not fee_ok:
            liquidity_fail_reasons.append(f"fee {fee_bps:.2f} bps > {self.max_fee_bps:.2f}")

        rsi_gap = self.state.twap_rsi_start_threshold - rsi
        rsi_score = max(0.0, min(1.0, rsi_gap / 35.0)) * 45.0
        if rsi <= 30:
            rsi_score += 25.0
            rsi_label = "oversold"
        elif rsi <= 45:
            rsi_score += 15.0
            rsi_label = "cooling"
        elif rsi <= self.state.twap_rsi_start_threshold:
            rsi_score += 6.0
            rsi_label = "near entry"
        else:
            rsi_label = "wait"

        volatility_score = min(volatility * 10000.0, 1.0) * 15.0
        liquidity_score = min(math.log10(max(quote_volume, 1.0)) / 9.0, 1.0) * 20.0
        momentum_score = 10.0 if -5.0 <= change_24h <= 5.0 else 3.0 if -10.0 <= change_24h <= 10.0 else -5.0
        fee_score_bonus = max(0.0, (self.max_fee_bps - fee_bps)) * 0.6
        filter_penalty = 0.0
        if quote_volume < self.min_quote_volume_24h_usdt and self.min_quote_volume_24h_usdt > 0:
            filter_penalty += min(12.0, 12.0 * (1.0 - (quote_volume / self.min_quote_volume_24h_usdt)))
        if spread_bps > self.max_spread_bps and self.max_spread_bps > 0:
            filter_penalty += min(15.0, 7.0 * ((spread_bps / self.max_spread_bps) - 1.0))
        if depth_notional < self.min_depth_notional_usdt and self.min_depth_notional_usdt > 0:
            filter_penalty += min(15.0, 15.0 * (1.0 - (depth_notional / self.min_depth_notional_usdt)))
        if not fee_ok:
            filter_penalty += min(10.0, max(0.0, fee_bps - self.max_fee_bps))

        liquidation_score = float((liquidation_signal or {}).get("score", 0.0) or 0.0)
        raw_score = rsi_score + volatility_score + liquidity_score + momentum_score + fee_score_bonus + liquidation_score
        score = round(max(0.0, raw_score - filter_penalty), 2)
        reason = (
            f"RSI {rsi:.1f} ({rsi_label}), 24h {change_24h:+.2f}%, "
            f"1m vol {volatility * 100:.2f}%, quote vol ${quote_volume / 1_000_000:.1f}M, "
            f"spread {spread_bps:.2f} bps, depth20 ${depth_notional:,.0f}, fee {fee_bps:.2f} bps"
        )
        if liquidation_signal:
            reason = f"{reason}, liquidation {liquidation_score:+.2f} ({liquidation_signal.get('bias', 'neutral')})"
        if liquidity_fail_reasons:
            reason = f"{reason}. Filter warning: " + "; ".join(liquidity_fail_reasons)
        return score, reason, liquidity_ok, spread_bps, depth_notional, fee_ok

    def _get_trade_fee_bps_map(self) -> dict[str, float]:
        now = time.time()
        if self._fee_cache and now - self._fee_cache[0] < 600:
            return self._fee_cache[1]

        fee_map: dict[str, float] = {}
        if self.dry_run:
            self._fee_cache = (now, fee_map)
            return fee_map

        try:
            response = self._send_signed_request("GET", "/sapi/v1/asset/tradeFee", {})
            if isinstance(response, list):
                for item in response:
                    symbol = str(item.get("symbol") or "").upper()
                    maker = float(item.get("makerCommission") or 0.0)
                    taker = float(item.get("takerCommission") or 0.0)
                    if symbol:
                        fee_map[symbol] = max(maker, taker) * 10000.0
        except Exception as exc:
            LOGGER.warning("Unable to load per-symbol trade fees: %s", exc)

        self._fee_cache = (now, fee_map)
        return fee_map

    def _get_book_ticker(self, symbol: str, timeout_seconds: float = 10) -> dict[str, Any]:
        now = time.time()
        cached = self._book_ticker_cache.get(symbol)
        if cached and now - cached[0] < 10:
            return cached[1]
        if self.primary_exchange == "bitget":
            payload = self._get_bitget_public_json(f"/api/v2/spot/market/tickers?symbol={urllib.parse.quote(symbol)}", timeout_seconds)
            item = (payload.get("data") or [{}])[0]
            result = {"bidPrice": str(item.get("bidPr") or "0"), "askPrice": str(item.get("askPr") or "0")}
            self._book_ticker_cache[symbol] = (now, result)
            return result
        query = urllib.parse.urlencode({"symbol": symbol})
        url = f"{BINANCE_BASE_URL}/api/v3/ticker/bookTicker?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self._book_ticker_cache[symbol] = (now, payload)
        return payload

    def _get_orderbook_depth(self, symbol: str, timeout_seconds: float = 10) -> dict[str, Any]:
        now = time.time()
        cached = self._depth_cache.get(symbol)
        if cached and now - cached[0] < 10:
            return cached[1]
        if self.primary_exchange == "bitget":
            query = urllib.parse.urlencode({"symbol": symbol, "type": "step0", "limit": 20})
            payload = self._get_bitget_public_json(f"/api/v2/spot/market/orderbook?{query}", timeout_seconds)
            result = (payload.get("data") or {})
            if isinstance(result, list):
                result = result[0] if result else {}
            self._depth_cache[symbol] = (now, result)
            return result
        query = urllib.parse.urlencode({"symbol": symbol, "limit": 20})
        url = f"{BINANCE_BASE_URL}/api/v3/depth?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self._depth_cache[symbol] = (now, payload)
        return payload

    def _bitget_symbol_candidates(self, symbol: str) -> list[str]:
        normalized = self._normalize_symbol(symbol)
        quote_asset = self._quote_asset_for_symbol(normalized)
        base_asset = normalized[:-len(quote_asset)] if normalized.endswith(quote_asset) else normalized
        candidates = [normalized]
        if base_asset and quote_asset != "USDT":
            candidates.append(f"{base_asset}USDT")
        return list(dict.fromkeys(candidates))

    def _futures_symbol_candidates(self, symbol: str) -> list[str]:
        normalized = self._normalize_symbol(symbol)
        quote_asset = self._quote_asset_for_symbol(normalized)
        base_asset = normalized[:-len(quote_asset)] if normalized.endswith(quote_asset) else normalized
        candidates = [normalized]
        if base_asset and quote_asset != "USDT":
            candidates.append(f"{base_asset}USDT")
        return list(dict.fromkeys(candidates))

    def _get_binance_futures_json(self, path: str, timeout_seconds: float) -> Any:
        request = urllib.request.Request(f"{BINANCE_FUTURES_BASE_URL}{path}", headers={"User-Agent": "volatility-twap-bot"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def _get_liquidation_signal(self, symbol: str, timeout_seconds: float = 2.0) -> dict[str, Any]:
        now = time.time()
        normalized = self._normalize_symbol(symbol)
        cached = self._liquidation_cache.get(normalized)
        if cached and now - cached[0] < 60:
            return cached[1]

        if self.is_stock_instrument(normalized):
            signal = {
                "source": "not_applicable",
                "symbol": normalized,
                "score": 0.0,
                "bias": "not_applicable",
                "notional_usdt": 0.0,
                "long_liq_usdt": 0.0,
                "short_liq_usdt": 0.0,
                "summary": "Crypto futures liquidation data is not applied to Reality stock tokens.",
            }
            self._liquidation_cache[normalized] = (now, signal)
            return signal

        last_error = "unavailable"
        for futures_symbol in self._futures_symbol_candidates(normalized):
            try:
                start_ms = int((now - 3600) * 1000)
                query = urllib.parse.urlencode({"symbol": futures_symbol, "startTime": start_ms, "limit": 100})
                orders = self._get_binance_futures_json(f"/fapi/v1/allForceOrders?{query}", timeout_seconds)
                if isinstance(orders, list) and orders:
                    long_liq = Decimal("0")
                    short_liq = Decimal("0")
                    for order in orders:
                        price = Decimal(str(order.get("avgPrice") or order.get("price") or "0"))
                        qty = Decimal(str(order.get("executedQty") or order.get("origQty") or "0"))
                        notional = price * qty
                        if str(order.get("side") or "").upper() == "SELL":
                            long_liq += notional
                        else:
                            short_liq += notional
                    total = long_liq + short_liq
                    if total > 0:
                        bias_value = float((short_liq - long_liq) / total)
                        magnitude = min(12.0, math.log10(float(total) + 10.0) * 1.5)
                        score = round(bias_value * magnitude, 2)
                        bias = "short_squeeze_bullish" if score > 1 else "long_liquidation_bearish" if score < -1 else "neutral"
                        signal = {
                            "source": f"Binance Futures liquidations {futures_symbol}",
                            "symbol": futures_symbol,
                            "score": score,
                            "bias": bias,
                            "notional_usdt": float(total),
                            "long_liq_usdt": float(long_liq),
                            "short_liq_usdt": float(short_liq),
                            "summary": f"1h liquidations ${float(total):,.0f}; long ${float(long_liq):,.0f}, short ${float(short_liq):,.0f}, bias {bias}",
                        }
                        self._liquidation_cache[normalized] = (now, signal)
                        return signal
            except Exception as exc:
                last_error = str(exc)[:120]

            try:
                flow_query = urllib.parse.urlencode({"symbol": futures_symbol, "period": "5m", "limit": 12})
                flow = self._get_binance_futures_json(f"/futures/data/takerlongshortRatio?{flow_query}", timeout_seconds)
                oi = self._get_binance_futures_json(f"/futures/data/openInterestHist?{flow_query}", timeout_seconds)
                if isinstance(flow, list) and flow and isinstance(oi, list) and len(oi) >= 2:
                    buy_vol = sum(Decimal(str(item.get("buyVol", "0") or "0")) for item in flow)
                    sell_vol = sum(Decimal(str(item.get("sellVol", "0") or "0")) for item in flow)
                    total_flow = buy_vol + sell_vol
                    flow_bias = float((buy_vol - sell_vol) / total_flow) if total_flow > 0 else 0.0
                    oi_start = Decimal(str(oi[0].get("sumOpenInterestValue", "0") or "0"))
                    oi_end = Decimal(str(oi[-1].get("sumOpenInterestValue", "0") or "0"))
                    oi_change_pct = float((oi_end - oi_start) / oi_start) if oi_start > 0 else 0.0
                    pressure = min(8.0, abs(flow_bias) * 6.0 + min(abs(oi_change_pct) * 100.0, 2.0))
                    direction = 1.0 if flow_bias > 0 else -1.0 if flow_bias < 0 else 0.0
                    # Falling OI with directional taker pressure often behaves like liquidation/short-squeeze unwind.
                    squeeze_boost = 1.25 if oi_change_pct < 0 else 0.75
                    score = round(direction * pressure * squeeze_boost, 2)
                    bias = "short_squeeze_pressure" if score > 1 else "long_liquidation_pressure" if score < -1 else "neutral"
                    signal = {
                        "source": f"Binance Futures flow/OI proxy {futures_symbol}",
                        "symbol": futures_symbol,
                        "score": score,
                        "bias": bias,
                        "notional_usdt": float(abs(oi_end - oi_start)),
                        "long_liq_usdt": 0.0,
                        "short_liq_usdt": 0.0,
                        "flow_bias": round(flow_bias, 4),
                        "open_interest_change_pct": round(oi_change_pct * 100.0, 4),
                        "summary": f"5m x12 futures proxy: taker bias {flow_bias:+.2%}, OI {oi_change_pct:+.2%}, {bias}",
                    }
                    self._liquidation_cache[normalized] = (now, signal)
                    return signal
            except Exception as exc:
                last_error = str(exc)[:120]

        signal = {
            "source": "unavailable",
            "symbol": normalized,
            "score": 0.0,
            "bias": "unavailable",
            "notional_usdt": 0.0,
            "long_liq_usdt": 0.0,
            "short_liq_usdt": 0.0,
            "summary": f"Liquidation signal unavailable: {last_error}",
        }
        self._liquidation_cache[normalized] = (now, signal)
        return signal

    def _get_bitget_public_json(self, path: str, timeout_seconds: float) -> dict[str, Any]:
        request = urllib.request.Request(f"{BITGET_BASE_URL}{path}", headers={"User-Agent": "volatility-twap-bot"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if str(payload.get("code", "00000")) != "00000":
            raise RuntimeError(f"Bitget public API error {payload.get('code')}: {payload.get('msg')}")
        return payload

    def _get_bitget_symbol_info(self, symbol: str) -> dict[str, Any]:
        payload = self._get_bitget_public_json(
            f"/api/v2/spot/public/symbols?symbol={urllib.parse.quote(self._normalize_symbol(symbol))}",
            10,
        )
        items = payload.get("data") or []
        if not items:
            raise RuntimeError(f"Bitget does not report spot symbol {symbol}")
        return dict(items[0])

    def _get_external_liquidity_snapshot(self, symbol: str, timeout_seconds: float = 2.0) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
        now = time.time()
        cached = self._external_liquidity_cache.get(symbol)
        if cached and now - cached[0] < 30:
            return cached[1]

        last_error: Exception | None = None
        for bitget_symbol in self._bitget_symbol_candidates(symbol):
            try:
                query = urllib.parse.urlencode({"symbol": bitget_symbol})
                ticker_payload = self._get_bitget_public_json(f"/api/v2/spot/market/tickers?{query}", timeout_seconds)
                ticker_data = ticker_payload.get("data") or []
                if isinstance(ticker_data, list):
                    ticker_data = ticker_data[0] if ticker_data else {}
                if not isinstance(ticker_data, dict) or not ticker_data:
                    raise RuntimeError("Bitget ticker response was empty")

                book_query = urllib.parse.urlencode({"symbol": bitget_symbol, "type": "step0", "limit": 20})
                depth_payload = self._get_bitget_public_json(f"/api/v2/spot/market/orderbook?{book_query}", timeout_seconds)
                depth_data = depth_payload.get("data") or {}
                if isinstance(depth_data, list):
                    depth_data = depth_data[0] if depth_data else {}
                if not isinstance(depth_data, dict) or not depth_data:
                    raise RuntimeError("Bitget orderbook response was empty")

                change_raw = float(ticker_data.get("change24h") or ticker_data.get("changeUtc") or 0.0)
                change_percent = change_raw * 100.0 if abs(change_raw) <= 2.0 else change_raw
                quote_volume = ticker_data.get("quoteVolume") or ticker_data.get("usdtVolume") or "0"
                last_price = ticker_data.get("lastPr") or ticker_data.get("close") or ticker_data.get("last") or "0"
                bids = depth_data.get("bids") or []
                asks = depth_data.get("asks") or []
                bid_price = ticker_data.get("bidPr") or (bids[0][0] if bids and len(bids[0]) >= 2 else "0")
                ask_price = ticker_data.get("askPr") or (asks[0][0] if asks and len(asks[0]) >= 2 else "0")
                normalized = (
                    {
                        "quoteVolume": str(quote_volume),
                        "priceChangePercent": str(change_percent),
                        "lastPrice": str(last_price),
                    },
                    {
                        "bidPrice": str(bid_price),
                        "askPrice": str(ask_price),
                    },
                    {
                        "bids": bids,
                        "asks": asks,
                    },
                    f"Bitget {bitget_symbol}",
                )
                self._external_liquidity_cache[symbol] = (now, normalized)
                return normalized
            except Exception as exc:
                last_error = exc
                continue
        raise RuntimeError(last_error or f"No external liquidity source available for {symbol}")

    def _get_24h_ticker(self, symbol: str, timeout_seconds: float = 10) -> dict[str, Any]:
        if self.primary_exchange == "bitget":
            ticker, _, _, _ = self._get_external_liquidity_snapshot(symbol, timeout_seconds)
            return ticker
        query = urllib.parse.urlencode({"symbol": symbol})
        url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))

    def get_price_candles(self, symbol: str | None = None, interval: str = "15m", limit: int = 96, timeout_seconds: float = 10) -> list[dict[str, Any]]:
        normalized = self._normalize_symbol(symbol)
        safe_interval = interval if interval in {"1m", "5m", "15m", "30m", "1h", "4h", "1d"} else "15m"
        safe_limit = max(10, min(500, int(limit)))
        if self.primary_exchange == "bitget":
            granularity = {"1m": "1min", "5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}[safe_interval]
            payload = self._get_bitget_public_json(
                f"/api/v2/spot/market/candles?symbol={urllib.parse.quote(normalized)}&granularity={granularity}&limit={safe_limit}",
                timeout_seconds,
            )
            raw_candles = list(reversed(payload.get("data") or []))
            return [
                {"open_time": int(item[0]), "open": float(item[1]), "high": float(item[2]), "low": float(item[3]), "close": float(item[4]), "volume": float(item[5]), "close_time": int(item[0])}
                for item in raw_candles if isinstance(item, list) and len(item) >= 6
            ]
        params = urllib.parse.urlencode({"symbol": normalized, "interval": safe_interval, "limit": safe_limit})
        url = f"{BINANCE_BASE_URL}/api/v3/klines?{params}"
        request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            candles = json.loads(response.read().decode("utf-8"))
        return [
            {
                "open_time": int(candle[0]),
                "open": float(Decimal(str(candle[1]))),
                "high": float(Decimal(str(candle[2]))),
                "low": float(Decimal(str(candle[3]))),
                "close": float(Decimal(str(candle[4]))),
                "volume": float(Decimal(str(candle[5]))),
                "close_time": int(candle[6]),
            }
            for candle in candles
            if isinstance(candle, list) and len(candle) >= 7
        ]

    def get_recent_trades(self, symbol: str | None = None, hours: float = 1.0, limit: int = 1000) -> list[dict[str, Any]]:
        normalized = self._normalize_symbol(symbol)
        safe_hours = max(0.1, min(24.0, float(hours)))
        safe_limit = max(1, min(1000, int(limit)))
        if self.dry_run:
            return []
        end_ms = int(time.time() * 1000)
        start_ms = int((time.time() - (safe_hours * 3600.0)) * 1000)
        raw_trades = self._send_signed_request("GET", "/api/v3/myTrades", {
            "symbol": normalized,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": safe_limit,
        })
        trades = []
        for trade in raw_trades if isinstance(raw_trades, list) else []:
            price = Decimal(str(trade.get("price", "0") or "0"))
            qty = Decimal(str(trade.get("qty", "0") or "0"))
            quote_qty = Decimal(str(trade.get("quoteQty", "0") or "0"))
            if quote_qty <= 0 and price > 0 and qty > 0:
                quote_qty = (price * qty).quantize(Decimal("0.00000001"))
            trades.append({
                "id": trade.get("id"),
                "order_id": trade.get("orderId"),
                "symbol": normalized,
                "time": int(trade.get("time", 0) or 0),
                "side": "BUY" if bool(trade.get("isBuyer")) else "SELL",
                "price": float(price),
                "qty": float(qty),
                "quote_qty": float(quote_qty),
                "commission": float(Decimal(str(trade.get("commission", "0") or "0"))),
                "commission_asset": str(trade.get("commissionAsset") or ""),
                "is_maker": bool(trade.get("isMaker")),
            })
        trades.sort(key=lambda item: item.get("time", 0), reverse=True)
        return trades

    def _load_state(self) -> BotState:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                if "copilot_cli_status" in data and "codex_cli_status" not in data:
                    data["codex_cli_status"] = data["copilot_cli_status"]
                data.pop("copilot_cli_status", None)
                # Restore Decimal fields from JSON strings
                for key in ("current_price", "position_qty", "position_free_qty", "position_locked_qty",
                           "position_cost_basis", "current_profit_usdt",
                           "target_profit_usdt", "target_profit_suggested_usdt", "target_sell_price", "target_sell_qty",
                           "stop_loss_price", "stop_loss_qty",
                           "target_profit_auto_apply_min_delta_usdt", "total_asset_value_usdt", "budget_total", "budget_remaining",
                           "equity_risk_per_trade_pct", "risk_stop_move_pct", "recommended_twap_quote_budget",
                           "daily_drawdown_limit_pct", "daily_equity_baseline_usdt",
                           "max_loss_usdt", "trailing_stop_usdt", "peak_profit_usdt",
                           "twap_auto_switch_budget_usdt", "min_twap_quote_budget_usdt", "max_twap_quote_budget_usdt",
                           "active_twap_quote_budget", "active_twap_executed_quote",
                           "adaptive_dca_min_step_pct", "adaptive_dca_max_step_pct", "adaptive_max_entry_balance_fraction",
                           "adaptive_stop_loss_pct"):
                    if key in data and isinstance(data[key], str):
                        data[key] = Decimal(data[key])
                if "entry_price" in data and isinstance(data["entry_price"], str):
                    data["entry_price"] = Decimal(data["entry_price"])
                return BotState(**data)
            except Exception as e:
                LOGGER.warning(f"Failed to load state: {e}")
        return BotState(status="idle")

    def _save_state(self) -> None:
        with self.lock:
            data = asdict(self.state)
            for key in ("position_qty", "position_free_qty", "position_locked_qty", "position_cost_basis", "current_profit_usdt",
                       "target_profit_usdt", "target_profit_suggested_usdt", "target_sell_price", "target_sell_qty",
                      "stop_loss_price", "stop_loss_qty",
                      "target_profit_auto_apply_min_delta_usdt", "total_asset_value_usdt", "budget_total", "budget_remaining",
                      "equity_risk_per_trade_pct", "risk_stop_move_pct", "recommended_twap_quote_budget",
                      "daily_drawdown_limit_pct", "daily_equity_baseline_usdt",
                      "max_loss_usdt", "trailing_stop_usdt", "peak_profit_usdt", "twap_auto_switch_budget_usdt",
                      "adaptive_dca_min_step_pct", "adaptive_dca_max_step_pct", "adaptive_max_entry_balance_fraction",
                      "adaptive_stop_loss_pct"):
                data[key] = str(data[key])
            data["entry_price"] = str(data["entry_price"]) if data["entry_price"] else None
            self.state_file.write_text(json.dumps(data, indent=2))

    def get_status(self) -> dict[str, Any]:
        """Return current bot status for dashboard"""
        codex_cli_status = self._check_codex_cli_status()
        supported_catalog = self.get_exchange_supported_symbols()
        supported_symbols = self.get_discovery_symbols(limit=100)
        if self.symbol in supported_catalog:
            supported_symbols = list(dict.fromkeys([self.symbol, *supported_symbols]))
        supported_instruments = [
            {
                "symbol": symbol,
                "instrument_type": self.get_instrument_type(symbol),
                "is_stock": self.is_stock_instrument(symbol),
            }
            for symbol in supported_symbols
        ]
        instrument_type = self.get_instrument_type()
        is_stock = self.is_stock_instrument()
        bitget_account_mode = "not_applicable"
        stock_trading_note = ""
        stock_live_order_eligible = False
        if is_stock and self.primary_exchange == "bitget":
            bitget_account_mode, account_mode_note = self._get_bitget_account_mode()
            stock_block_reason = self._stock_trading_block_reason()
            stock_live_order_eligible = bitget_account_mode == "uta"
            stock_trading_note = stock_block_reason or (
                f"{account_mode_note}. Reality order placement also requires Bitget UID whitelisting."
            )
        with self.lock:
            self.state.codex_cli_status = codex_cli_status
            return {
                "status": self.state.status,
                "symbol": self.symbol,
                "primary_exchange": self.primary_exchange,
                "base_asset": self._base_asset(),
                "instrument_type": instrument_type,
                "is_stock": is_stock,
                "underlying_ticker": self._stock_underlying_ticker() if is_stock else self._base_asset(),
                "bitget_account_mode": bitget_account_mode,
                "stock_live_order_eligible": stock_live_order_eligible,
                "stock_trading_note": stock_trading_note,
                "supported_symbols": supported_symbols,
                "supported_instruments": supported_instruments,
                "dry_run": self.dry_run,
                "execution_mode": "PAPER TRADING" if self.dry_run else "LIVE",
                "started_at": self.state.started_at,
                "running_duration_seconds": self._running_duration_seconds(),
                "volatility": round(self.state.current_volatility * 100, 3),
                "volatility_threshold": round(self.state.volatility_threshold * 100, 3),
                "is_volatile": self.state.is_volatile,
                "current_rsi": round(self.state.current_rsi, 2),
                "rsi_threshold": self.state.rsi_threshold,
                "rsi_ready": self.state.current_rsi < self.state.rsi_threshold,
                "market_sentiment": round(self.state.market_sentiment, 1),
                "sentiment_name": self.state.sentiment_name,
                "sentiment_ready": self.state.market_sentiment < self.state.sentiment_entry_threshold,
                "media_sentiment_score": round(self.state.media_sentiment_score, 1),
                "media_sentiment_label": self.state.media_sentiment_label,
                "media_sentiment_summary": self.state.media_sentiment_summary,
                "media_news_headlines": self.state.media_news_headlines,
                "liquidation_pressure_score": round(self.state.liquidation_pressure_score, 2),
                "liquidation_bias": self.state.liquidation_bias,
                "liquidation_notional_usdt": round(self.state.liquidation_notional_usdt, 2),
                "liquidation_summary": self.state.liquidation_summary,
                "advisor_action": self.state.advisor_action,
                "advisor_confidence": round(self.state.advisor_confidence, 1),
                "advisor_summary": self.state.advisor_summary,
                "advisor_source": self.state.advisor_source,
                "advisor_updated_at": self.state.advisor_updated_at,
                "strategy_analysis_updated_at": self.state.strategy_analysis_updated_at,
                "strategy_analysis_source": self.state.strategy_analysis_source,
                "strategy_analysis_summary": self.state.strategy_analysis_summary,
                "adaptive_dca_levels": self.state.adaptive_dca_levels,
                "adaptive_dca_reprice_interval_seconds": self.state.adaptive_dca_reprice_interval_seconds,
                "adaptive_dca_min_step_pct": float(self.state.adaptive_dca_min_step_pct),
                "adaptive_dca_max_step_pct": float(self.state.adaptive_dca_max_step_pct),
                "adaptive_max_entry_balance_fraction": float(self.state.adaptive_max_entry_balance_fraction),
                "codex_cli_status": codex_cli_status,
                "codex_model": os.getenv("CODEX_CLI_MODEL", "gpt-5.4-mini"),
                "codex_strategy_interval_seconds": self.strategy_analysis_interval_seconds,
                "codex_max_calls_per_hour": max(0, int(os.getenv("CODEX_CLI_MAX_CALLS_PER_HOUR", "1"))),
                "codex_active_only": self.codex_active_only,
                "codex_direct_fallback_enabled": self.codex_direct_fallback_enabled,
                "total_asset_value_usdt": float(self.state.total_asset_value_usdt),
                "total_asset_count": self.state.total_asset_count,
                "total_asset_summary": self.state.total_asset_summary,
                "total_asset_updated_at": self.state.total_asset_updated_at,
                "current_price": float(self.state.current_price),
                "position_qty": float(self.state.position_qty),
                "position_free_qty": float(self.state.position_free_qty),
                "position_locked_qty": float(self.state.position_locked_qty),
                "position_cost_basis": float(self.state.position_cost_basis),
                "cost_basis_source": self.state.cost_basis_source,
                "entry_price": float(self.state.entry_price) if self.state.entry_price else None,
                "current_profit_usdt": float(self.state.current_profit_usdt),
                "target_profit_usdt": float(self.state.target_profit_usdt),
                "target_profit_suggested_usdt": float(self.state.target_profit_suggested_usdt),
                "target_profit_suggestion_reason": self.state.target_profit_suggestion_reason,
                "target_profit_suggestion_source": self.state.target_profit_suggestion_source,
                "target_profit_suggestion_updated_at": self.state.target_profit_suggestion_updated_at,
                "target_profit_auto_apply": self.state.target_profit_auto_apply,
                "target_profit_auto_apply_min_delta_usdt": float(self.state.target_profit_auto_apply_min_delta_usdt),
                "target_profit_last_auto_applied_at": self.state.target_profit_last_auto_applied_at,
                "target_profit_auto_apply_note": self.state.target_profit_auto_apply_note,
                "target_sell_order_id": self.state.target_sell_order_id,
                "target_sell_client_order_id": self.state.target_sell_client_order_id,
                "target_sell_status": self.state.target_sell_status,
                "target_sell_price": float(self.state.target_sell_price),
                "target_sell_qty": float(self.state.target_sell_qty),
                "target_sell_interval_seconds": self.state.target_sell_interval_seconds,
                "target_sell_last_checked_at": self.state.target_sell_last_checked_at,
                "stop_loss_order_id": self.state.stop_loss_order_id,
                "stop_loss_client_order_id": self.state.stop_loss_client_order_id,
                "stop_loss_status": self.state.stop_loss_status,
                "stop_loss_price": float(self.state.stop_loss_price),
                "stop_loss_qty": float(self.state.stop_loss_qty),
                "stop_loss_last_checked_at": self.state.stop_loss_last_checked_at,
                "max_order_loss_usdt": float(self.max_order_loss_usdt),
                "adaptive_stop_loss_pct": round(float(self.state.adaptive_stop_loss_pct) * 100.0, 2),
                "adaptive_stop_loss_usdt": float(self._cycle_max_loss_usdt()),
                "adaptive_strategy_mode": self.state.adaptive_strategy_mode,
                "budget_total": float(self.state.budget_total),
                "budget_remaining": float(self.state.budget_remaining),
                "equity_risk_per_trade_pct": float(self.state.equity_risk_per_trade_pct * Decimal("100")),
                "risk_stop_move_pct": float(self.state.risk_stop_move_pct * Decimal("100")),
                "recommended_twap_quote_budget": float(self.state.recommended_twap_quote_budget),
                "last_risk_note": self.state.last_risk_note,
                "daily_drawdown_limit_pct": float(self.state.daily_drawdown_limit_pct * Decimal("100")),
                "daily_equity_baseline_usdt": float(self.state.daily_equity_baseline_usdt),
                "daily_drawdown_pct": round(self.state.daily_drawdown_pct * 100.0, 3),
                "daily_drawdown_triggered": self.state.daily_drawdown_triggered,
                "daily_drawdown_day": self.state.daily_drawdown_day,
                "cycles_completed": self.state.cycles_completed,
                "peak_profit_usdt": float(self.state.peak_profit_usdt),
                "can_switch_symbol": self._can_switch_symbol_locked(),
                "can_start": self.state.status in ("idle", "completed", "error"),
                "can_stop": self.state.status in ("trading", "waiting"),
            }

    def _running_duration_seconds(self) -> float:
        if not self.state.started_at or self.state.status not in {"waiting", "trading"}:
            return 0.0
        return max(0.0, time.time() - self.state.started_at)

    def start(self, budget_usdt: float = 5000, duration_hours: float = 12.0) -> str:
        """Start the bot in a background thread"""
        with self.lock:
            if self.running:
                return "Bot is already running"
            if self.state.status not in ("idle", "completed", "error"):
                return f"Cannot start bot in {self.state.status} state"
            
            self.state.budget_total = Decimal(str(budget_usdt))
            self.state.budget_remaining = Decimal(str(budget_usdt))
            self.state.duration_hours = duration_hours
            self.state.started_at = time.time()
            self.state.status = "waiting"
            self.state.user_requested_start = True
            self.state.position_qty = Decimal("0")
            self.state.position_cost_basis = Decimal("0")
            self.state.entry_price = None
            self.state.current_profit_usdt = Decimal("0")
            self._save_state()
            
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            return "Bot started"

    def set_target_profit(self, target_usdt: float | str, source: str = "manual") -> str:
        target = Decimal(str(target_usdt)).quantize(Decimal("0.01"))
        if target < Decimal("1"):
            raise ValueError("Target profit must be at least 1 USDT")
        if target > Decimal("500"):
            raise ValueError("Target profit must be 500 USDT or less")
        with self.lock:
            if target != self.state.target_profit_usdt:
                cancel_target_order = getattr(self, "_cancel_target_sell_order", None)
                if callable(cancel_target_order):
                    cancel_target_order()
            self.state.target_profit_usdt = target
            self.state.target_profit_suggestion_source = source
            self.state.target_sell_last_checked_at = 0.0
            self._save_state()
        return f"Target profit set to ${float(target):.2f}"

    def stop(self) -> str:
        """Stop the bot gracefully"""
        with self.lock:
            self.running = False
            self.state.user_requested_start = False
            self.state.user_requested_stop = True
            self.state.last_exit_reason = "Bot stopped by user"
            if self.state.position_cost_basis < getattr(self, "dust_position_cost_usdt", Decimal("5.00")) and not self.state.active_twap_algo_id:
                self.state.status = "idle"
                self.state.active_twap_status = "idle"
            self._save_state()
        if self.thread:
            self.thread.join(timeout=5)
        return "Bot stopped"

    def restart(self) -> str:
        """Restart the bot after completion or error"""
        self.stop()
        time.sleep(0.5)
        with self.lock:
            self.state.status = "idle"
            self.state.user_requested_stop = False
            self.state.cycles_completed += 1
            self._save_state()
        return "Bot restarted"

    def _run_loop(self) -> None:
        """Main bot loop — if position open at end, sells before stopping"""
        try:
            end_time = time.time() + (self.state.duration_hours * 3600)
            while self.running and time.time() < end_time:
                try:
                    self._step()
                    time.sleep(30)
                except Exception as e:
                    LOGGER.error(f"Bot error: {e}")
                    with self.lock:
                        self.state.status = "error"
                        self._save_state()
                    break

            # Graceful exit: sell any open position before stopping
            with self.lock:
                if self.state.status == "trading" and self.state.position_qty > 0:
                    LOGGER.info("Bot duration ended — closing open position")
                    profit = self.state.current_profit_usdt
                    self.state.status = "completed"
                    self._place_sell_order()
                    self._send_telegram_message(
                        f"⏰ *BOT CYCLE ENDED — POSITION CLOSED*\n\n"
                        f"Profit: ${float(profit):.2f}\n"
                        f"Cycles: {self.state.cycles_completed}"
                    )
                elif self.state.status != "error":
                    self.state.status = "completed"
                    self._save_state()
        finally:
            self.running = False

    def _step(self) -> None:
        """Single iteration — market data OUTSIDE lock, state updates INSIDE lock"""
        # Gather market data without holding the lock (dashboard stays responsive)
        price = self._get_price()
        volatility, rsi = self._get_market_data()
        sentiment, sentiment_name = self._get_sentiment()

        with self.lock:
            self.state.current_price = price
            self.state.current_volatility = volatility
            self.state.current_rsi = rsi
            self.state.market_sentiment = sentiment
            self.state.sentiment_name = sentiment_name
            self.state.is_volatile = volatility >= self.state.volatility_threshold

            pnl = Decimal("0")
            exit_reason = None

            if self.state.position_qty > 0 and self.state.entry_price:
                pnl = (price - self.state.entry_price) * self.state.position_qty
                self.state.current_profit_usdt = pnl
                if pnl > self.state.peak_profit_usdt:
                    self.state.peak_profit_usdt = pnl

            if self.state.status == "waiting":
                volatility_ready = self.state.is_volatile
                rsi_ready = rsi < self.state.rsi_threshold
                sentiment_ready = sentiment < self.state.sentiment_entry_threshold
                budget_ok = self.state.budget_remaining > Decimal("0")

                if volatility_ready and rsi_ready and sentiment_ready and budget_ok:
                    LOGGER.info(
                        f"SIGNAL: Vol {volatility*100:.2f}% | RSI {rsi:.2f} | "
                        f"Sentiment {sentiment_name} — ENTERING"
                    )
                    self.state.status = "trading"
                    self.state.entry_time = time.time()
                    self.state.peak_profit_usdt = Decimal("0")
                    self._place_buy_order()
                    entry_msg = (
                        f"🚀 *BOT ENTERED TRADING POSITION*\n\n"
                        f"📊 Entry: ${float(self.state.current_price):,.2f}\n"
                        f"📈 Size: {float(self.state.position_qty):.8f} {self._base_asset()}\n"
                        f"💰 Cost: ${float(self.state.position_cost_basis):.2f}\n"
                        f"🎯 Target: ${float(self.state.target_profit_usdt):.2f}\n\n"
                        f"📉 Vol: {volatility*100:.2f}%\n"
                        f"📊 RSI: {rsi:.2f} (Oversold)\n"
                        f"💚 Sentiment: {sentiment_name.replace('_', ' ').title()}"
                    )
                    self._send_telegram_message(entry_msg)
                else:
                    reasons = []
                    if not volatility_ready:
                        reasons.append(f"Vol:{volatility*100:.2f}%")
                    if not rsi_ready:
                        reasons.append(f"RSI:{rsi:.2f}")
                    if not sentiment_ready:
                        reasons.append(f"Sent:{sentiment_name}")
                    if reasons:
                        LOGGER.info(f"Waiting: {', '.join(reasons)}")

            elif self.state.status == "trading":
                # === EXIT CONDITIONS (HIGH-GAIN EDGE) ===
                if pnl >= self.state.target_profit_usdt:
                    exit_reason = f"🎯 Profit target: +${float(pnl):.2f}"
                elif self.state.peak_profit_usdt >= self.state.trailing_stop_usdt:
                    trail_drop = self.state.peak_profit_usdt - pnl
                    if trail_drop >= self.state.trailing_stop_usdt:
                        exit_reason = (
                            f"🔒 Trailing lock: +${float(pnl):.2f} "
                            f"(peak was +${float(self.state.peak_profit_usdt):.2f})"
                        )
                elif pnl <= -self.state.max_loss_usdt:
                    exit_reason = f"🛑 Stop-loss: {float(pnl):.2f}"
                elif rsi > 70 and pnl > Decimal("0"):
                    exit_reason = f"📊 RSI overbought ({rsi:.1f}) locking +${float(pnl):.2f}"
                elif time.time() - self.state.entry_time > self.state.max_position_hours * 3600:
                    exit_reason = f"⏰ Time limit: {'+$' if pnl > 0 else ''}${float(pnl):.2f} after {self.state.max_position_hours:.0f}h"

                if exit_reason:
                    LOGGER.info(f"Exiting: {exit_reason}")
                    self.state.status = "completed"
                    self._place_sell_order()
                    self._send_telegram_message(
                        f"✅ *BOT EXITED POSITION*\n\n{exit_reason}"
                    )

            self._save_state()

    def _place_buy_order(self) -> None:
        """Place a buy order — dry_run or live via Binance signed API"""
        qty = self.state.budget_remaining / Decimal(str(self.state.current_price))
        was_dry = self.dry_run

        if was_dry:
            LOGGER.info(f"DRY RUN: Bought {float(qty):.8f} {self._base_asset()} at ${float(self.state.current_price):,.2f}")
        else:
            LOGGER.info(f"PLACING BUY: {float(qty):.8f} {self._base_asset()} at market")
            try:
                self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quoteOrderQty": format(self.state.budget_remaining, "f"),
                    "newOrderRespType": "FULL",
                })
                LOGGER.info(f"LIVE BUY FILLED: {float(qty):.8f} {self._base_asset()}")
            except Exception as e:
                LOGGER.error(f"LIVE BUY FAILED: {e}")
                self.state.status = "error"
                self._send_telegram_message(f"❌ *BUY ORDER FAILED*\n\n{str(e)[:300]}")
                return

        self.state.position_qty = qty
        self.state.entry_price = Decimal(str(self.state.current_price))
        self.state.position_cost_basis = self.state.budget_remaining
        self.state.budget_remaining = Decimal("0")

    def _place_sell_order(self) -> None:
        """Place a sell order — dry_run or live via Binance signed API"""
        if self.state.position_qty <= 0:
            return

        qty = self.state.position_free_qty if self.state.position_free_qty > 0 else self.state.position_qty
        qty = self._round_quantity(qty)
        if qty <= 0:
            return
        price = self.state.current_price
        realized = qty * Decimal(str(price))
        was_dry = self.dry_run

        if was_dry:
            LOGGER.info(f"DRY RUN: Sold {float(qty):.8f} {self._base_asset()} at ${float(price):,.2f}")
        else:
            LOGGER.info(f"PLACING SELL: {float(qty):.8f} {self._base_asset()} at market")
            try:
                self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": format(qty, "f"),
                    "newOrderRespType": "FULL",
                })
                LOGGER.info(f"LIVE SELL FILLED: {float(qty):.8f} {self._base_asset()}")
            except Exception as e:
                LOGGER.error(f"LIVE SELL FAILED: {e}")
                self.state.status = "error"
                self._send_telegram_message(f"❌ *SELL ORDER FAILED*\n\n{str(e)[:300]}")
                return

        self.state.budget_remaining = realized
        self.state.position_qty = Decimal("0")
        self.state.position_free_qty = Decimal("0")
        self.state.position_locked_qty = Decimal("0")
        self.state.position_cost_basis = Decimal("0")
        self.state.cost_basis_source = "none"

    def _get_price(self, symbol: str | None = None, timeout_seconds: float = 10) -> Decimal:
        """Get current symbol price from Binance"""
        symbol = self._normalize_symbol(symbol)
        try:
            if self.primary_exchange == "bitget":
                payload = self._get_bitget_public_json(f"/api/v2/spot/market/tickers?symbol={urllib.parse.quote(symbol)}", timeout_seconds)
                items = payload.get("data") or []
                return Decimal(str(items[0].get("lastPr") or items[0].get("close") or "0"))
            query = urllib.parse.urlencode({"symbol": symbol})
            url = f"{BINANCE_BASE_URL}/api/v3/ticker/price?{query}"
            request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                data = json.loads(response.read().decode("utf-8"))
                return Decimal(str(data.get("price", "0")))
        except Exception as e:
            LOGGER.error(f"Failed to get price: {e}")
            return Decimal(str(self.state.current_price))

    def _get_market_data(self, symbol: str | None = None, timeout_seconds: float = 10) -> tuple[float, float]:
        """Single Binance klines call — returns (volatility, wilder_rsi)"""
        symbol = self._normalize_symbol(symbol)
        try:
            if self.primary_exchange == "bitget":
                payload = self._get_bitget_public_json(
                    f"/api/v2/spot/market/candles?symbol={urllib.parse.quote(symbol)}&granularity=1min&limit=30",
                    timeout_seconds,
                )
                candles = payload.get("data") or []
                closes = [Decimal(str(item[4])) for item in candles if isinstance(item, list) and len(item) >= 5]
                closes.reverse()
                if len(closes) < 2:
                    return 0.0, 50.0
                returns = [float((closes[i] / closes[i - 1]) - Decimal("1")) for i in range(1, len(closes))]
                return statistics.pstdev(returns) if returns else 0.0, self._calc_wilder_rsi(closes, period=14)
            params = urllib.parse.urlencode({"symbol": symbol, "interval": "1m", "limit": 30})
            url = f"{BINANCE_BASE_URL}/api/v3/klines?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                candles = json.loads(resp.read().decode("utf-8"))
                closes = [Decimal(str(c[4])) for c in candles if len(c) >= 5]

            if len(closes) < 2:
                return 0.0, 50.0

            # Volatility from ALL 30 candles
            returns = [float((closes[i] / closes[i-1]) - Decimal("1")) for i in range(1, len(closes))]
            volatility = statistics.pstdev(returns) if returns else 0.0

            # Wilder's smoothed RSI from same data
            rsi = self._calc_wilder_rsi(closes, period=14)

            return volatility, rsi
        except Exception as e:
            LOGGER.error(f"Failed to get market data: {e}")
            return self.state.current_volatility, self.state.current_rsi

    @staticmethod
    def _calc_wilder_rsi(closes: list[Decimal], period: int = 14) -> float:
        """Wilder's smoothed RSI — matches TradingView/Binance exactly"""
        if len(closes) < period + 1:
            return 50.0

        # Initial SMA for first avg_gain / avg_loss
        gains, losses = [], []
        for i in range(1, period + 1):
            ch = closes[i] - closes[i-1]
            gains.append(float(ch) if ch > 0 else 0.0)
            losses.append(float(abs(ch)) if ch < 0 else 0.0)

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        # Wilder's smoothing for remaining candles
        for i in range(period + 1, len(closes)):
            ch = closes[i] - closes[i-1]
            gain = float(ch) if ch > 0 else 0.0
            loss = float(abs(ch)) if ch < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    def _get_sentiment(self) -> tuple[float, str]:
        """Get market sentiment from Crypto Fear and Greed Index"""
        if self.is_stock_instrument():
            # Crypto Fear & Greed is unrelated to an equity's underlying
            # business. Stock decisions use RSI and stock-specific news instead.
            return 50.0, "stock_neutral"
        try:
            # Free API: crypto fear and greed index
            url = "https://api.alternative.me/fng/?limit=1"
            request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("data"):
                    sentiment_value = float(data["data"][0]["value"])
                    
                    # Classify sentiment
                    if sentiment_value < 25:
                        name = "extreme_fear"
                    elif sentiment_value < 45:
                        name = "fear"
                    elif sentiment_value < 55:
                        name = "neutral"
                    elif sentiment_value < 75:
                        name = "greed"
                    else:
                        name = "extreme_greed"
                    
                    return sentiment_value, name
        except Exception as e:
            LOGGER.error(f"Failed to get sentiment: {e}")
        
        # Return cached or neutral sentiment on error
        return self.state.market_sentiment, self.state.sentiment_name

    def _get_media_sentiment(self) -> tuple[float, str, str]:
        now = time.time()
        if self._media_cache and now - self._media_cache_at < 600:
            return self._media_cache

        is_stock = self.is_stock_instrument()
        subject = self._stock_underlying_ticker() if is_stock else self._base_asset()
        query_text = f"{subject} stock earnings market" if is_stock else f"{subject} crypto market OR Binance"
        query = urllib.parse.quote(query_text)
        url = f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en"
        if is_stock:
            positive = {"beat", "upgrade", "growth", "profit", "rally", "gain", "record", "strong", "buyback", "outperform"}
            negative = {"miss", "downgrade", "decline", "loss", "drop", "fall", "selloff", "weak", "layoff", "underperform"}
        else:
            positive = {"surge", "rally", "bull", "gain", "record", "inflow", "breakout", "safe haven", "strong", "accumulate"}
            negative = {"drop", "fall", "bear", "loss", "outflow", "crash", "selloff", "risk", "weak", "liquidation"}
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                feed = response.read()
            root = ET.fromstring(feed)
            titles = [item.findtext("title", default="").strip() for item in root.findall(".//item")[:10]]
            titles = [title for title in titles if title]
            text = " ".join(titles).lower()
            pos_hits = sum(1 for word in positive if word in text)
            neg_hits = sum(1 for word in negative if word in text)
            score = max(0.0, min(100.0, 50.0 + (pos_hits - neg_hits) * 8.0))
            if score >= 60:
                label = "positive"
            elif score <= 40:
                label = "negative"
            else:
                label = "neutral"
            headlines = " | ".join(titles[:5])[:900]
            context = "stock" if is_stock else "crypto"
            summary = f"{len(titles)} {context} headlines scanned, +{pos_hits}/-{neg_hits} sentiment keywords. Headlines: {headlines}"
            self._media_headlines_cache = headlines
        except Exception as exc:
            LOGGER.warning("Unable to load media sentiment: %s", exc)
            score = self.state.media_sentiment_score
            label = self.state.media_sentiment_label
            summary = "Media sentiment unavailable; using last cached value"
            self._media_headlines_cache = self.state.media_news_headlines

        self._media_cache = (score, label, summary)
        self._media_cache_at = now
        return self._media_cache

    def _check_codex_cli_status(self) -> str:
        now = time.time()
        if self._codex_cli_status_cache and now - self._codex_cli_status_cache_at < 60:
            return self._codex_cli_status_cache
        if os.getenv("CODEX_CLI_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
            self._codex_cli_status_cache = "unavailable_install_required"
            self._codex_cli_status_cache_at = now
            return self._codex_cli_status_cache
        if os.getenv("CODEX_CLI_BINARY", "").strip():
            self._codex_cli_status_cache = "available"
            self._codex_cli_status_cache_at = now
            return "available"

        # Check if the daemon has already produced a recent response (daemon running as user)
        responses_dir = self.root_dir.parents[1] / "runtime" / "codex_responses"
        try:
            if any(responses_dir.glob("*.json")):
                self._codex_cli_status_cache = "available"
                self._codex_cli_status_cache_at = now
                return "available"
        except OSError:
            pass

        # File-existence check for the binary (avoids spawning subprocesses from IIS context)
        candidates = [
            os.getenv("CODEX_CLI_BINARY", "").strip(),
            str(Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"),
            *[
                str(path)
                for path in sorted(
                    (Path.home() / ".vscode" / "extensions").glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
                    key=lambda item: item.stat().st_mtime if item.exists() else 0,
                    reverse=True,
                )
            ],
        ]
        for c in candidates:
            try:
                exists = bool(c and Path(c).exists())
            except OSError:
                exists = False
            if exists:
                self._codex_cli_status_cache = "available"
                self._codex_cli_status_cache_at = now
                return "available"

        self._codex_cli_status_cache = "unavailable_install_required"
        self._codex_cli_status_cache_at = now
        return "unavailable_install_required"

    def _check_codex_cli_status_UNUSED_subprocess(self) -> str:
        """Legacy subprocess-based check kept for reference — not used from IIS."""
        now = time.time()
        candidates = [
            os.getenv("CODEX_CLI_BINARY", "").strip(),
            "codex",
            str(Path.home() / "AppData" / "Roaming" / "npm" / "codex.cmd"),
        ]
        candidates = [item for item in candidates if item]

        last_error = ""
        try:
            status = "unavailable_install_required"
            for executable in candidates:
                try:
                    if executable.lower().endswith((".cmd", ".bat")):
                        result = subprocess.run(
                            [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/c", executable, "--help"],
                            capture_output=True,
                            text=True,
                            timeout=8,
                        )
                    else:
                        result = subprocess.run(
                            [executable, "--help"],
                            capture_output=True,
                            text=True,
                            timeout=8,
                        )
                except Exception as exc:
                    last_error = exc.__class__.__name__
                    continue
                output = f"{result.stdout}\n{result.stderr}".lower()
                if "cannot find codex cli" in output or "install codex cli" in output:
                    status = "unavailable_install_required"
                    continue
                if result.returncode == 0 and "codex cli" in output:
                    status = "available"
                    break
                if result.returncode == 0:
                    status = "available"
                    break
                status = "unavailable"
        except Exception as exc:
            status = f"unavailable: {exc.__class__.__name__}"
        if status.startswith("unavailable") and last_error:
            status = f"{status}:{last_error}"
        self._codex_cli_status_cache = status
        self._codex_cli_status_cache_at = now
        return status

    def _update_advisor_signal(self) -> None:
        pnl = self.state.current_profit_usdt
        rsi = self.state.current_rsi
        fear_greed = self.state.market_sentiment
        media_score = self.state.media_sentiment_score
        is_stock = self.is_stock_instrument()
        sentiment_entry_ok = is_stock or fear_greed <= self.state.sentiment_entry_threshold
        codex_status = self._check_codex_cli_status()
        suggestion = self._update_target_profit_suggestion(codex_status)

        if self.state.position_qty > 0:
            if pnl >= self.state.target_profit_usdt:
                action = "SELL_PROFIT_TARGET"
                confidence = 95.0
                summary = f"Profit target reached at ${float(pnl):.2f}; sell free {self._base_asset()} balance."
            elif rsi >= 70 and pnl > 0:
                action = "SELL_RSI_STRENGTH"
                confidence = 82.0
                summary = f"RSI is overbought at {rsi:.1f} with positive P&L ${float(pnl):.2f}; lock profit."
            elif pnl <= -self.state.max_loss_usdt:
                action = "SELL_STOP_LOSS"
                confidence = 90.0
                summary = f"Loss guard triggered at ${float(pnl):.2f}; cut risk."
            elif rsi <= 35 and (is_stock or fear_greed <= 30) and media_score >= 40:
                action = "HOLD_DEEP_VALUE"
                confidence = 72.0
                market_context = "stock-specific news" if is_stock else f"fear {fear_greed:.0f}"
                summary = f"Holding {float(self.state.position_qty):.6f} {self._base_asset()}: low RSI {rsi:.1f}, {market_context}, media {self.state.media_sentiment_label}."
            else:
                action = "HOLD_MONITOR"
                confidence = float(max(58.0, min(90.0, suggestion.get("confidence", 62.0))))
                summary = (
                    f"Hold and monitor: P&L ${float(pnl):.2f}, RSI {rsi:.1f}, media {self.state.media_sentiment_label}. "
                    f"Adaptive target ${float(self.state.target_profit_usdt):.2f} ({suggestion.get('regime', 'balanced')})."
                )
        else:
            best = self._candidate_cache.get("best") if self._candidate_cache else None
            if rsi <= self.state.twap_rsi_start_threshold and sentiment_entry_ok and media_score >= 35:
                action = "TWAP_ENTRY_OK"
                confidence = float(max(70.0, min(92.0, suggestion.get("confidence", 76.0))))
                sentiment_context = "stock news" if is_stock else f"fear/greed {fear_greed:.0f}"
                summary = (
                    f"Entry conditions are acceptable for {self.symbol}: RSI {rsi:.1f}, {sentiment_context}, "
                    f"media {self.state.media_sentiment_label}, suggested target ${float(self.state.target_profit_usdt):.2f}."
                )
            elif best:
                action = "WAIT_OR_SWITCH_CANDIDATE"
                confidence = 58.0
                summary = f"No open {self._base_asset()} position. Current best ranked pair is {best['symbol']} with score {best['score']:.1f}."
            else:
                action = "WAIT"
                confidence = 50.0
                summary = f"Waiting for lower RSI and better reward/risk. Current RSI {rsi:.1f}."

        self.state.advisor_action = action
        self.state.advisor_confidence = confidence
        self.state.advisor_summary = summary
        self.state.advisor_source = f"{suggestion.get('source', 'rules')}; codex_cli={codex_status}"
        self.state.advisor_updated_at = time.time()
        self.state.codex_cli_status = codex_status

    def _classify_market_regime(self, rsi: float, fear_greed: float, media_score: float, volatility_pct: float) -> str:
        if rsi <= 35 and fear_greed <= 30 and media_score >= 40:
            return "accumulation"
        if rsi >= 67 or fear_greed >= 72:
            return "distribution"
        if volatility_pct >= 0.16:
            return "high_volatility"
        if 45 <= rsi <= 60 and fear_greed >= 45 and media_score >= 55:
            return "trend"
        return "balanced"

    def _build_research_target_suggestion(self, reference_value: Decimal) -> dict[str, Any]:
        volatility_pct = max(0.0, self.state.current_volatility * 100.0)
        rsi = self.state.current_rsi
        fear_greed = self.state.market_sentiment
        media_score = self.state.media_sentiment_score
        regime = self._classify_market_regime(rsi, fear_greed, media_score, volatility_pct)

        base_pct = Decimal("0.005")
        vol_multiplier = 1.0 + min(max(volatility_pct, 0.01), 0.30) * 1.8
        regime_multiplier = {
            "accumulation": 1.35,
            "trend": 1.20,
            "balanced": 1.00,
            "high_volatility": 1.10,
            "distribution": 0.75,
        }.get(regime, 1.0)
        fear_media_bias = 1.0
        if fear_greed <= 25:
            fear_media_bias += 0.10
        elif fear_greed >= 75:
            fear_media_bias -= 0.12
        if media_score >= 60:
            fear_media_bias += 0.08
        elif media_score <= 40:
            fear_media_bias -= 0.08

        target_pct = base_pct * Decimal(str(vol_multiplier * regime_multiplier * fear_media_bias))
        target_pct = max(Decimal("0.003"), min(target_pct, Decimal("0.035")))
        suggested = (reference_value * target_pct).quantize(Decimal("0.01"))
        suggested = max(Decimal("3.00"), min(suggested, Decimal("200.00")))

        confidence = 60.0
        if regime in {"accumulation", "trend"}:
            confidence += 14.0
        if media_score >= 60:
            confidence += 6.0
        if rsi <= 35:
            confidence += 5.0
        confidence = max(50.0, min(confidence, 92.0))

        reason = (
            f"Regime={regime}, vol={volatility_pct:.3f}%, RSI={rsi:.1f}, fear/greed={fear_greed:.0f}, "
            f"media={self.state.media_sentiment_label}."
        )
        return {
            "target_profit_usdt": suggested,
            "target_pct": target_pct,
            "confidence": confidence,
            "reason": reason,
            "regime": regime,
            "source": "research_regime_vol_targeting",
            "action": "HOLD_MONITOR",
        }

    def _extract_json_object(self, text: str) -> dict[str, Any] | None:
        match = re.search(r"\{[\s\S]*\}", text)
        if not match:
            return None
        try:
            payload = json.loads(match.group(0))
            if isinstance(payload, dict):
                return payload
        except Exception:
            return None
        return None

    def _codex_advisory_eligible(self) -> bool:
        """Reserve paid Codex advice for the pair that is actually carrying risk."""
        if not self.codex_active_only:
            return True
        return bool(
            self.state.status == "trading"
            or self.state.position_qty > 0
            or self.state.position_cost_basis >= self.dust_position_cost_usdt
            or self.state.active_twap_algo_id
            or self.state.target_sell_order_id
            or self.state.stop_loss_order_id
        )

    def _ask_codex_cli_decision(self, snapshot: dict[str, Any], baseline: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        if not self.codex_decision_enabled:
            return None, "decision_disabled"

        # ------------------------------------------------------------------ #
        # Daemon path: write a request file and poll for the response.         #
        # The daemon (tools/codex_daemon.py) runs as the logged-in user so    #
        # it has proper Codex auth. The IIS process just reads files.         #
        # ------------------------------------------------------------------ #
        runtime_root = self.root_dir.parents[1] / "runtime"
        requests_dir = runtime_root / "codex_requests"
        responses_dir = runtime_root / "codex_responses"
        symbol_lower = self.symbol.lower()
        req_file = requests_dir / f"{symbol_lower}.json"
        resp_file = responses_dir / f"{symbol_lower}.json"

        # Check for a recent cached response first (within analysis interval)
        try:
            if resp_file.exists():
                cached = json.loads(resp_file.read_text(encoding="utf-8"))
                age = time.time() - float(cached.get("responded_at", 0))
                if age < self.strategy_analysis_interval_seconds and cached.get("payload"):
                    return cached["payload"], "decision_available"
        except Exception:
            pass

        # Write request for the daemon
        try:
            requests_dir.mkdir(parents=True, exist_ok=True)
            req_data = {
                "symbol": symbol_lower,
                "requested_at": time.time(),
                "codex_eligible": True,
                "priority": 2 if self.state.position_qty > 0 or self.state.position_cost_basis >= self.dust_position_cost_usdt else 1,
                "snapshot": snapshot,
                "baseline": {
                    "target_profit_usdt": float(baseline["target_profit_usdt"]),
                    "target_pct": float(baseline["target_pct"]),
                    "regime": baseline["regime"],
                    "confidence": baseline["confidence"],
                    "dca_levels": baseline.get("dca_levels"),
                    "dca_min_step_pct": float(baseline.get("dca_min_step_pct", self.state.adaptive_dca_min_step_pct)),
                    "dca_max_step_pct": float(baseline.get("dca_max_step_pct", self.state.adaptive_dca_max_step_pct)),
                    "dca_reprice_interval_seconds": baseline.get("dca_reprice_interval_seconds"),
                    "max_entry_balance_fraction": float(baseline.get("max_entry_balance_fraction", self.state.adaptive_max_entry_balance_fraction)),
                },
            }
            req_file.write_text(json.dumps(req_data), encoding="utf-8")
        except Exception as exc:
            return None, f"decision_failed:request_write:{exc.__class__.__name__}"

        # Poll up to half the decision timeout for the daemon to respond
        poll_deadline = time.time() + self.codex_daemon_poll_seconds
        while time.time() < poll_deadline:
            time.sleep(1)
            try:
                if resp_file.exists():
                    resp = json.loads(resp_file.read_text(encoding="utf-8"))
                    if float(resp.get("responded_at", 0)) >= req_data["requested_at"]:
                        payload = resp.get("payload")
                        status = resp.get("status", "decision_invalid_output")
                        return payload, status
            except Exception:
                pass

        # ------------------------------------------------------------------ #
        # Fallback: try spawning the CLI directly if daemon didn't respond.   #
        # This works in standalone (non-IIS) deployments.                     #
        # ------------------------------------------------------------------ #
        if not self.codex_direct_fallback_enabled:
            return None, "decision_pending_daemon"
        if not self.codex_decision_command:
            return None, "decision_failed:daemon_timeout_no_command"

        prompt_text = json.dumps(
            {
                "task": "Return strict JSON only. Analyze the market and suggest bounded strategy parameters.",
                "allowed_keys": [
                    "action", "confidence", "target_profit_usdt", "target_pct", "reason", "regime",
                    "dca_levels", "dca_min_step_pct", "dca_max_step_pct", "dca_reprice_interval_seconds",
                    "max_entry_balance_fraction", "stop_loss_pct", "strategy_mode",
                ],
                "constraints": {
                    "max_target_pct": 0.035,
                    "min_target_pct": 0.003,
                    "target_profit_min_usdt": 3,
                    "target_profit_max_usdt": 200,
                    "entry_rsi_must_remain_below": self.strict_entry_rsi,
                    "max_order_loss_usdt_hard_cap": float(self.max_order_loss_usdt),
                    "stop_loss_pct_min_max": [0.005, 0.05],
                    "stop_loss_pct_current": float(self.state.adaptive_stop_loss_pct),
                    "strategy_mode_options": ["swing", "momentum", "defensive", "mean_reversion"],
                    "strategy_mode_current": self.state.adaptive_strategy_mode,
                    "max_entry_balance_fraction_cap": float(self.max_entry_balance_fraction),
                    "dca_levels_min_max": [1, self.dca_levels],
                    "dca_step_pct_min_max": [float(self.dca_min_step_pct), float(self.dca_max_step_pct)],
                    "dca_reprice_seconds_min_max": [60, 600],
                    "never_disable_risk_controls": True,
                    "never_change_live_or_paper_mode": True,
                    "never_recommend_entry_when_rsi_at_or_above_threshold": True,
                    "use_crypto_news_context": not self.is_stock_instrument(),
                    "use_stock_news_context": self.is_stock_instrument(),
                },
                "market": snapshot,
                "baseline": req_data["baseline"],
            },
            separators=(",", ":"),
        )
        try:
            if "{prompt}" in self.codex_decision_command:
                escaped = prompt_text.replace('"', '\\"')
                command_line = self.codex_decision_command.replace("{prompt}", escaped)
                run_args = [os.environ.get("COMSPEC", r"C:\Windows\System32\cmd.exe"), "/c", command_line]
                result = subprocess.run(run_args, capture_output=True, text=True, timeout=self.codex_decision_timeout_seconds)
            else:
                run_args = shlex.split(self.codex_decision_command, posix=False)
                result = subprocess.run(
                    run_args, input=prompt_text, capture_output=True, text=True,
                    timeout=self.codex_decision_timeout_seconds,
                )
        except Exception as exc:
            return None, f"decision_failed:{exc.__class__.__name__}"

        raw_output = (result.stdout or "") + "\n" + (result.stderr or "")
        payload = self._extract_json_object(raw_output)
        if result.returncode != 0 or payload is None:
            return None, f"decision_invalid_output:{result.returncode}"
        return payload, "decision_available"

    # Strategy mode presets: each defines DCA params + stop-loss pct
    _STRATEGY_MODE_PRESETS: dict[str, dict] = {
        "swing": {          # Default: patient DCA accumulation, moderate stop
            "dca_levels": 3, "dca_min_step_pct": 0.003, "dca_max_step_pct": 0.012,
            "dca_reprice_interval_seconds": 240, "max_entry_balance_fraction": 0.15,
            "stop_loss_pct": 0.020,
        },
        "momentum": {       # Fast-moving market: fewer DCA, tighter target, faster reprice
            "dca_levels": 2, "dca_min_step_pct": 0.002, "dca_max_step_pct": 0.008,
            "dca_reprice_interval_seconds": 120, "max_entry_balance_fraction": 0.20,
            "stop_loss_pct": 0.015,
        },
        "defensive": {      # Uncertain market: minimal exposure, wide stop to survive dips
            "dca_levels": 2, "dca_min_step_pct": 0.004, "dca_max_step_pct": 0.015,
            "dca_reprice_interval_seconds": 360, "max_entry_balance_fraction": 0.06,
            "stop_loss_pct": 0.030,
        },
        "mean_reversion": { # Deep oversold only: tight entry, small target, tight stop
            "dca_levels": 4, "dca_min_step_pct": 0.004, "dca_max_step_pct": 0.018,
            "dca_reprice_interval_seconds": 180, "max_entry_balance_fraction": 0.10,
            "stop_loss_pct": 0.025,
        },
    }

    def _build_strategy_parameter_baseline(self, regime: str, volatility_pct: float, rsi: float, fear_greed: float, media_score: float) -> dict[str, Any]:
        if volatility_pct >= 0.16:
            levels = min(self.dca_levels, 4)
            max_step = self.dca_max_step_pct
            min_step = max(self.dca_min_step_pct, Decimal("0.005"))
            reprice = 120
            entry_fraction = min(self.max_entry_balance_fraction, Decimal("0.12"))
            stop_loss_pct = Decimal("0.030")  # high vol: wider stop
            reason = "High volatility: wider DCA spacing, faster repricing, wider stop-loss"
        elif rsi < self.strict_entry_rsi and fear_greed <= 30 and media_score >= 40:
            levels = min(self.dca_levels, 4)
            max_step = min(self.dca_max_step_pct, Decimal("0.012"))
            min_step = max(self.dca_min_step_pct, Decimal("0.003"))
            reprice = 180
            entry_fraction = min(self.max_entry_balance_fraction, Decimal("0.20"))
            stop_loss_pct = Decimal("0.025")  # oversold: allow room for more dip
            reason = "Oversold/fear: DCA accumulation, 2.5% stop allows for further dip"
        elif regime == "distribution" or rsi >= 60:
            levels = min(self.dca_levels, 2)
            max_step = min(self.dca_max_step_pct, Decimal("0.006"))
            min_step = self.dca_min_step_pct
            reprice = 300
            entry_fraction = min(self.max_entry_balance_fraction, Decimal("0.05"))
            stop_loss_pct = Decimal("0.012")  # distribution: tight stop
            reason = "Distribution/high RSI: defensive settings, tight 1.2% stop-loss"
        else:
            levels = min(self.dca_levels, 3)
            max_step = min(self.dca_max_step_pct, Decimal("0.010"))
            min_step = self.dca_min_step_pct
            reprice = 240
            entry_fraction = min(self.max_entry_balance_fraction, Decimal("0.10"))
            stop_loss_pct = Decimal("0.020")  # balanced: 2% stop
            reason = "Balanced conditions: moderate DCA settings, 2% stop-loss"

        return {
            "dca_levels": levels,
            "dca_min_step_pct": min_step,
            "dca_max_step_pct": max_step,
            "dca_reprice_interval_seconds": reprice,
            "max_entry_balance_fraction": entry_fraction,
            "stop_loss_pct": stop_loss_pct,
            "strategy_reason": reason,
        }

    def _apply_strategy_parameter_decision(self, decision: dict[str, Any], source: str) -> None:
        before = {
            "levels": self.state.adaptive_dca_levels,
            "min_step": self.state.adaptive_dca_min_step_pct,
            "max_step": self.state.adaptive_dca_max_step_pct,
            "reprice": self.state.adaptive_dca_reprice_interval_seconds,
            "entry_fraction": self.state.adaptive_max_entry_balance_fraction,
            "stop_pct": self.state.adaptive_stop_loss_pct,
        }
        try:
            # If Codex sets a strategy_mode, apply the full preset first
            mode = str(decision.get("strategy_mode") or "").strip().lower()
            if mode and mode in self._STRATEGY_MODE_PRESETS:
                preset = self._STRATEGY_MODE_PRESETS[mode]
                self.state.adaptive_strategy_mode = mode
                decision = {**preset, **{k: v for k, v in decision.items() if v is not None and k != "strategy_mode"}}

            if decision.get("dca_levels") is not None:
                self.state.adaptive_dca_levels = int(decision.get("dca_levels"))
            if decision.get("dca_reprice_interval_seconds") is not None:
                self.state.adaptive_dca_reprice_interval_seconds = int(decision.get("dca_reprice_interval_seconds"))
            if decision.get("dca_min_step_pct") is not None:
                self.state.adaptive_dca_min_step_pct = Decimal(str(decision.get("dca_min_step_pct")))
            if decision.get("dca_max_step_pct") is not None:
                self.state.adaptive_dca_max_step_pct = Decimal(str(decision.get("dca_max_step_pct")))
            if decision.get("max_entry_balance_fraction") is not None:
                self.state.adaptive_max_entry_balance_fraction = Decimal(str(decision.get("max_entry_balance_fraction")))
            # Dynamic stop-loss: apply with guardrails (0.5% – 5% of position)
            if decision.get("stop_loss_pct") is not None:
                proposed_sl = Decimal(str(decision.get("stop_loss_pct")))
                current_sl = self.state.adaptive_stop_loss_pct
                # Allow up to 1.5x / down to 0.6x per cycle, clamped to [0.005, 0.05]
                bounded_sl = max(current_sl * Decimal("0.60"), min(proposed_sl, current_sl * Decimal("1.50")))
                self.state.adaptive_stop_loss_pct = max(Decimal("0.005"), min(bounded_sl, Decimal("0.05")))
        except Exception as exc:
            self.state.strategy_analysis_source = f"{source}+parameter_parse_error:{exc.__class__.__name__}"
        self._normalize_adaptive_strategy_parameters()
        after = {
            "levels": self.state.adaptive_dca_levels,
            "min_step": self.state.adaptive_dca_min_step_pct,
            "max_step": self.state.adaptive_dca_max_step_pct,
            "reprice": self.state.adaptive_dca_reprice_interval_seconds,
            "entry_fraction": self.state.adaptive_max_entry_balance_fraction,
            "stop_pct": self.state.adaptive_stop_loss_pct,
        }
        # Compute dollar stop at current position size
        reference = self.state.position_cost_basis if self.state.position_cost_basis > 0 else self.state.active_twap_quote_budget
        dynamic_stop_usdt = (reference * after['stop_pct']).quantize(Decimal("0.01")) if reference > 0 else Decimal("0")
        reason = str(decision.get("reason") or decision.get("strategy_reason") or "Strategy parameters refreshed")[:260]
        self.state.strategy_analysis_updated_at = time.time()
        self.state.strategy_analysis_source = source
        mode_tag = f" [{self.state.adaptive_strategy_mode}]" if self.state.adaptive_strategy_mode != "swing" else ""
        self.state.strategy_analysis_summary = (
            f"{reason}{mode_tag}. DCA {before['levels']}->{after['levels']}, "
            f"step {float(before['min_step']):.3%}-{float(before['max_step']):.3%} -> {float(after['min_step']):.3%}-{float(after['max_step']):.3%}, "
            f"reprice {before['reprice']}s->{after['reprice']}s, "
            f"entry cap {float(before['entry_fraction']):.0%}->{float(after['entry_fraction']):.0%}. "
            f"Stop-loss {float(before['stop_pct']):.1%}->{float(after['stop_pct']):.1%}"
            + (f" (≈${float(dynamic_stop_usdt):.2f} on ${float(reference):.0f} position)" if reference > 0 else "") + "."
        )

    def _apply_target_guardrails(self, proposed_usdt: Decimal, reference_value: Decimal) -> Decimal:
        proposed = max(Decimal("3.00"), min(proposed_usdt.quantize(Decimal("0.01")), Decimal("200.00")))
        current = self.state.target_profit_usdt
        if current <= 0:
            current = Decimal("10.00")

        step_up = current * Decimal("1.40")
        step_down = current * Decimal("0.65")
        bounded_step = max(step_down, min(proposed, step_up))

        ref_cap = max(Decimal("3.00"), reference_value * Decimal("0.05"))
        return min(bounded_step, ref_cap).quantize(Decimal("0.01"))

    def _update_target_profit_suggestion(self, codex_status: str) -> dict[str, Any]:
        reference_value = self.state.position_cost_basis
        if reference_value <= 0:
            reference_value = self.state.active_twap_quote_budget
        if reference_value <= 0:
            reference_value = self.state.max_twap_quote_budget_usdt

        baseline = self._build_research_target_suggestion(reference_value)
        baseline.update(self._build_strategy_parameter_baseline(
            baseline["regime"],
            self.state.current_volatility * 100.0,
            self.state.current_rsi,
            self.state.market_sentiment,
            self.state.media_sentiment_score,
        ))
        final_decision = dict(baseline)

        snapshot = {
            "symbol": self.symbol,
            "instrument_type": self.get_instrument_type(),
            "underlying_ticker": self._stock_underlying_ticker() if self.is_stock_instrument() else self._base_asset(),
            "price": float(self.state.current_price),
            "position_qty": float(self.state.position_qty),
            "position_cost_basis_usdt": float(self.state.position_cost_basis),
            "current_profit_usdt": float(self.state.current_profit_usdt),
            "rsi": self.state.current_rsi,
            "volatility_pct": self.state.current_volatility * 100.0,
            "fear_greed": self.state.market_sentiment,
            "media_sentiment_score": self.state.media_sentiment_score,
            "media_sentiment_label": self.state.media_sentiment_label,
            "media_sentiment_summary": self.state.media_sentiment_summary,
            "news_headlines": self.state.media_news_headlines,
            "crypto_news_headlines": self.state.media_news_headlines,
            "liquidation_pressure_score": self.state.liquidation_pressure_score,
            "liquidation_bias": self.state.liquidation_bias,
            "liquidation_notional_usdt": self.state.liquidation_notional_usdt,
            "liquidation_summary": self.state.liquidation_summary,
            "equity_usdt": float(self.state.total_asset_value_usdt),
            "daily_drawdown_pct": self.state.daily_drawdown_pct * 100.0,
            "daily_drawdown_triggered": self.state.daily_drawdown_triggered,
            "adaptive_stop_loss_pct": float(self.state.adaptive_stop_loss_pct),
            "adaptive_strategy_mode": self.state.adaptive_strategy_mode,
            "current_stop_loss_usdt": float(self._cycle_max_loss_usdt()),
        }

        codex_note = ""
        now = time.time()
        analysis_due = now - self.state.strategy_analysis_updated_at >= self.strategy_analysis_interval_seconds
        codex_eligible = self._codex_advisory_eligible()
        if codex_status == "available" and analysis_due and codex_eligible:
            cli_payload, cli_state = self._ask_codex_cli_decision(snapshot, baseline)
            codex_note = cli_state
            if cli_payload:
                try:
                    raw_target = cli_payload.get("target_profit_usdt")
                    if raw_target is None and cli_payload.get("target_pct") is not None:
                        raw_target = reference_value * Decimal(str(cli_payload.get("target_pct")))
                    if raw_target is not None:
                        proposed_target = Decimal(str(raw_target))
                        guarded = self._apply_target_guardrails(proposed_target, reference_value)
                        final_decision["target_profit_usdt"] = guarded
                    if cli_payload.get("target_pct") is not None:
                        final_decision["target_pct"] = Decimal(str(cli_payload.get("target_pct")))
                    if cli_payload.get("action"):
                        final_decision["action"] = str(cli_payload.get("action"))[:64]
                    if cli_payload.get("confidence") is not None:
                        final_decision["confidence"] = float(cli_payload.get("confidence"))
                    if cli_payload.get("reason"):
                        final_decision["reason"] = str(cli_payload.get("reason"))[:320]
                    if cli_payload.get("regime"):
                        final_decision["regime"] = str(cli_payload.get("regime"))[:64]
                    final_decision["source"] = "codex_cli_decision_guardrailed"
                except Exception as exc:
                    final_decision["source"] = f"research_regime_vol_targeting+codex_parse_error:{exc.__class__.__name__}"
        elif codex_status == "available" and not codex_eligible:
            codex_note = "analysis_skipped_inactive_pair"
        elif codex_status == "available":
            codex_note = f"analysis_throttled_{int(self.strategy_analysis_interval_seconds - (now - self.state.strategy_analysis_updated_at))}s"
        else:
            codex_note = codex_status

        if analysis_due:
            self._apply_strategy_parameter_decision(final_decision, str(final_decision.get("source", "rules")))

        suggested = Decimal(str(final_decision["target_profit_usdt"]))
        reason = (
            f"{final_decision['reason']} Reference ${float(reference_value):.2f}. "
            f"codex={codex_note}."
        )

        self.state.target_profit_suggested_usdt = suggested
        self.state.target_profit_suggestion_reason = reason
        self.state.target_profit_suggestion_source = str(final_decision.get("source", "research_regime_vol_targeting"))
        self.state.target_profit_suggestion_updated_at = time.time()
        self._auto_apply_target_profit_suggestion(suggested, self.state.target_profit_suggestion_source)
        return final_decision

    def _auto_apply_target_profit_suggestion(self, suggested: Decimal, source: str) -> None:
        if not self.state.target_profit_auto_apply:
            self.state.target_profit_auto_apply_note = "Auto target is disabled"
            return
        delta = abs(suggested - self.state.target_profit_usdt)
        if delta < self.state.target_profit_auto_apply_min_delta_usdt:
            self.state.target_profit_auto_apply_note = (
                f"Auto target unchanged; suggestion is within ${float(self.state.target_profit_auto_apply_min_delta_usdt):.2f}."
            )
            return

        previous = self.state.target_profit_usdt
        self.state.target_profit_usdt = suggested
        self.state.target_profit_suggestion_source = f"{source}+auto_applied"
        self.state.target_profit_last_auto_applied_at = time.time()
        self.state.target_sell_last_checked_at = 0.0
        self.state.target_profit_auto_apply_note = (
            f"Auto-applied target ${float(suggested):.2f} from ${float(previous):.2f}; "
            "target sell will refresh on the next order check."
        )

    def _send_telegram_message(self, message: str) -> None:
        """Send a notification message via Telegram Bot API"""
        if not self.notifications_enabled or not self.telegram_api_key or not self.telegram_chat_id:
            return
        
        try:
            url = f"https://api.telegram.org/bot{self.telegram_api_key}/sendMessage"
            params = {
                "chat_id": self.telegram_chat_id,
                "text": message,
                "parse_mode": "Markdown",
            }
            query_string = urllib.parse.urlencode(params)
            full_url = f"{url}?{query_string}"
            request = urllib.request.Request(full_url, headers={"User-Agent": "volatility-twap-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.loads(response.read().decode("utf-8"))
                if result.get("ok"):
                    LOGGER.info(f"Telegram notification sent successfully")
                else:
                    LOGGER.warning(f"Telegram API error: {result.get('description', 'Unknown')}")
        except Exception as e:
            LOGGER.error(f"Failed to send Telegram message: {e}")

    def _send_signed_request(self, method: str, path: str, params: dict) -> dict:
        """Send a signed Binance REST API request (HMAC-SHA256)"""
        query = urllib.parse.urlencode(params)
        ts = int(time.time() * 1000)
        query += f"&timestamp={ts}&recvWindow=5000"
        sig = hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{BINANCE_BASE_URL}{path}?{query}&signature={sig}"
        # Only send a body for POST/PUT; GET/DELETE must have no body.
        body = b"" if method.upper() in ("POST", "PUT") else None
        req = urllib.request.Request(
            url,
            data=body,
            headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "volatility-twap-bot"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance {method} {path} failed: HTTP {exc.code} {raw}") from exc


def create_bot_app(root_dir: Path, bot: VolatilityTwapBot) -> dict[str, Any]:
    """Create Flask endpoints for bot control"""
    endpoints = {}
    
    def get_bot_status():
        s = bot.get_status()
        # Add extra computed fields for the dashboard
        s["sentiment_ready"] = s.get("market_sentiment", 100) < bot.state.sentiment_entry_threshold
        s["peak_profit_usdt"] = float(bot.state.peak_profit_usdt)
        s["entry_time"] = bot.state.entry_time
        s["max_position_hours"] = bot.state.max_position_hours
        return s
    
    def start_trading():
        budget = float(json.loads(request.data or "{}").get("budget", 5000))
        duration = float(json.loads(request.data or "{}").get("duration_hours", 12))
        message = bot.start(budget, duration)
        return {"message": message, "status": bot.get_status()}
    
    def stop_trading():
        message = bot.stop()
        return {"message": message, "status": bot.get_status()}
    
    def restart_trading():
        message = bot.restart()
        return {"message": message, "status": bot.get_status()}
    
    return {
        "status": get_bot_status,
        "start": start_trading,
        "stop": stop_trading,
        "restart": restart_trading,
    }


class TwapSwingBot(VolatilityTwapBot):
    """Always-on PAXGUSDT swing bot using native Binance Spot Algo TWAP entries."""

    symbol = "PAXGUSDT"

    def switch_symbol(self, symbol: str) -> str:
        requested = self._normalize_symbol(symbol)
        normalized = self.resolve_exchange_symbol(requested)
        with self.lock:
            if normalized == self.symbol:
                if requested != normalized:
                    return f"{requested} maps to {normalized} on {self.primary_exchange.upper()}; already using {normalized}"
                return f"Already using {normalized}"
            if not self._can_switch_symbol_locked():
                raise RuntimeError("Cannot switch pair while a live TWAP or position is active. Close it first.")
            if self.state.active_twap_algo_id:
                self._cancel_active_twap()
            if self.state.target_sell_order_id:
                self._cancel_target_sell_order()
            if self.state.stop_loss_order_id:
                self._cancel_stop_loss_order()
            self.symbol = normalized
            self.state.symbol = normalized
            self._quantity_step_size_cache = None
            self._price_tick_size_cache = None
            self._candidate_cache = None
            self._reset_cycle_state(keep_cycle_count=True)
            self.state.current_price = Decimal("0")
            self.state.current_volatility = 0.0
            self.state.current_rsi = 50.0
            self.state.status = "waiting" if self.running and not self.state.user_requested_stop else "idle"
            self._save_state()
        if requested != normalized:
            return f"Mapped unsupported {requested} to {normalized} on {self.primary_exchange.upper()}"
        return f"Switched TWAP pair to {normalized}"

    def _load_state(self) -> BotState:
        state = super()._load_state()
        if state.user_requested_stop:
            state.user_requested_start = False
        elif self.dry_run and state.status in {"waiting", "trading"}:
            state.status = "idle"
            state.user_requested_start = False
            state.user_requested_stop = False
        elif state.status in {"waiting", "trading"}:
            state.user_requested_start = True
            state.user_requested_stop = False
        return state

    def _save_state(self) -> None:
        with self.lock:
            data = asdict(self.state)
            for key in (
                "current_price", "position_qty", "position_free_qty", "position_locked_qty",
                "position_cost_basis", "current_profit_usdt",
                "target_profit_usdt", "target_profit_suggested_usdt", "target_sell_price", "target_sell_qty",
                "stop_loss_price", "stop_loss_qty",
                "target_profit_auto_apply_min_delta_usdt", "total_asset_value_usdt", "budget_total", "budget_remaining",
                "equity_risk_per_trade_pct", "risk_stop_move_pct", "recommended_twap_quote_budget",
                "daily_drawdown_limit_pct", "daily_equity_baseline_usdt",
                "max_loss_usdt", "trailing_stop_usdt", "peak_profit_usdt",
                "twap_auto_switch_budget_usdt", "min_twap_quote_budget_usdt", "max_twap_quote_budget_usdt",
                "active_twap_quote_budget", "active_twap_executed_quote",
                "adaptive_dca_min_step_pct", "adaptive_dca_max_step_pct", "adaptive_max_entry_balance_fraction",
                "adaptive_stop_loss_pct",
            ):
                data[key] = str(data[key])
            data["entry_price"] = str(data["entry_price"]) if data["entry_price"] else None
            self.state_file.write_text(json.dumps(data, indent=2))

    def set_strategy_mode(self, strategy_mode: str, twap_switch_budget_usdt: float | None = None) -> str:
        normalized = str(strategy_mode or "").strip().lower()
        if normalized not in {"auto", "spot", "twap"}:
            raise ValueError("strategy_mode must be one of: auto, spot, twap")
        with self.lock:
            self.state.strategy_mode = normalized
            if twap_switch_budget_usdt is not None:
                threshold = Decimal(str(twap_switch_budget_usdt)).quantize(Decimal("0.01"))
                if threshold <= 0:
                    raise ValueError("twap_switch_budget_usdt must be greater than 0")
                self.state.twap_auto_switch_budget_usdt = threshold
            self.state.strategy_note = (
                "Manual TWAP strategy selected"
                if normalized == "twap"
                else "Manual spot market strategy selected"
                if normalized == "spot"
                else "Auto strategy: use spot entry for small budgets, switch to TWAP when budget is sufficient"
            )
            self._save_state()
        return f"Strategy mode set to {normalized.upper()}"

    def set_primary_exchange(self, exchange: str) -> str:
        normalized = str(exchange or "").strip().lower()
        if normalized not in {"binance", "bitget"}:
            raise ValueError("primary exchange must be BINANCE or BITGET")
        with self.lock:
            if self.running or self.state.status in {"waiting", "trading"} or self.state.position_qty > 0:
                raise RuntimeError("Stop the bot and close the active position before changing the primary exchange.")
            if (
                normalized == "bitget"
                and not self.dry_run
                and not all((self.bitget_api_key, self.bitget_api_secret, self.bitget_passphrase))
            ):
                raise RuntimeError("BITGET_AccessAPIKey, BITGET_secretkey, and BITGET_PASSWORD must be set in .env")
            previous_exchange = self.primary_exchange
            previous_state_exchange = self.state.primary_exchange
            previous_symbols_cache = self._exchange_symbols_cache
            previous_instruments_cache = self._exchange_instruments_cache
            self.primary_exchange = normalized
            self.state.primary_exchange = normalized
            self._exchange_symbols_cache = None
            self._exchange_instruments_cache = None
            try:
                resolved_symbol = self.resolve_exchange_symbol(self.symbol, force=True)
            except Exception:
                self.primary_exchange = previous_exchange
                self.state.primary_exchange = previous_state_exchange
                self._exchange_symbols_cache = previous_symbols_cache
                self._exchange_instruments_cache = previous_instruments_cache
                raise
            if resolved_symbol != self.symbol:
                self.primary_exchange = previous_exchange
                self.state.primary_exchange = previous_state_exchange
                self._exchange_symbols_cache = previous_symbols_cache
                self._exchange_instruments_cache = previous_instruments_cache
                raise ValueError(
                    f"{self.symbol} is not tradable on {normalized.upper()}. "
                    f"Use {resolved_symbol} before switching exchanges."
                )
            self.primary_exchange_file.parent.mkdir(parents=True, exist_ok=True)
            self.primary_exchange_file.write_text(json.dumps({"primary_exchange": normalized, "updated_at": time.time()}, indent=2), encoding="utf-8")
            self._volume_symbols_cache = None
            self._quantity_step_size_cache = None
            self._price_tick_size_cache = None
            self._candidate_cache = None
            self._save_state()
        return f"Primary exchange set to {normalized.upper()}"

    def _resolve_entry_strategy(self) -> tuple[str, str, Decimal]:
        quote_budget = self._twap_quote_budget()
        if self.primary_exchange == "bitget":
            return "dca_buy", "Bitget spot mode: use timed DCA limit orders because Binance native TWAP is unavailable", quote_budget
        selected_mode = (self.state.strategy_mode or "auto").lower()
        if selected_mode == "twap":
            return "twap", "Manual TWAP strategy selected", quote_budget
        if selected_mode == "spot":
            return "spot", "Manual spot market strategy selected", quote_budget

        switch_budget = self.state.twap_auto_switch_budget_usdt
        if switch_budget <= 0:
            switch_budget = self.state.min_twap_quote_budget_usdt

        if quote_budget >= switch_budget:
            return "twap", f"Auto selected TWAP: budget ${float(quote_budget):.2f} >= ${float(switch_budget):.2f}", quote_budget
        return "dca_buy", f"Auto selected DCA buy ladder: budget ${float(quote_budget):.2f} < ${float(switch_budget):.2f}", quote_budget

    def _start_spot_cycle(self, quote_budget: Decimal | None = None) -> None:
        manual_override_active = self.state.manual_entry_override_until > time.time()
        if self.state.current_rsi >= self.strict_entry_rsi and not manual_override_active:
            self.state.active_twap_status = "waiting_rsi"
            self.state.last_exit_reason = f"Entry blocked: RSI {self.state.current_rsi:.2f} is not below {self.strict_entry_rsi:.0f}"
            return
        price = Decimal(str(self.state.current_price))
        if price <= 0:
            self.state.last_exit_reason = "Spot start skipped because price is unavailable"
            return

        if quote_budget is None:
            quote_budget = self._twap_quote_budget()
        if quote_budget <= 0:
            self.state.last_exit_reason = "Risk sizing returned zero budget; skipping spot start"
            return

        cycle_risk_usdt = (self._current_equity_usdt() * self.state.equity_risk_per_trade_pct).quantize(Decimal("0.01"))
        if cycle_risk_usdt > Decimal("0"):
            self.state.max_loss_usdt = self._cycle_max_loss_usdt()

        self._start_dca_buy_cycle(quote_budget)
        return

        if not self.dry_run:
            free_quote = self._quote_asset_free_balance()
            required_quote = (quote_budget * Decimal("0.98")).quantize(Decimal("0.01"))
            if free_quote < required_quote:
                self.state.active_twap_status = "waiting_quote_balance"
                self.state.last_exit_reason = (
                    f"Insufficient {self._quote_asset_for_symbol()} free balance ({float(free_quote):.2f}) for "
                    f"spot budget {float(quote_budget):.2f}"
                )
                return

        if self.dry_run:
            qty = self._round_quantity(quote_budget / price)
            executed_quote = (qty * price).quantize(Decimal("0.01")) if qty > 0 else Decimal("0")
        else:
            try:
                response = self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quoteOrderQty": format(quote_budget, "f"),
                    "newOrderRespType": "FULL",
                })
            except Exception as exc:
                self.state.active_twap_status = "start_failed"
                self.state.last_exit_reason = f"Spot start failed: {str(exc)[:180]}"
                return
            if isinstance(response, dict) and response.get("code") not in (None, 0, "0"):
                self.state.active_twap_status = "start_rejected"
                self.state.last_exit_reason = f"Spot market buy rejected: {response}"
                return
            qty = self._decimal_from_fields(response, "executedQty", "origQty")
            executed_quote = self._decimal_from_fields(response, "cummulativeQuoteQty")

        if qty <= 0:
            qty = self._round_quantity(quote_budget / price)
        if executed_quote <= 0:
            executed_quote = (qty * price).quantize(Decimal("0.01")) if qty > 0 else Decimal("0")
        if qty <= 0 or executed_quote <= 0:
            self.state.active_twap_status = "start_rejected"
            self.state.last_exit_reason = "Spot start produced zero fill"
            return

        self.state.status = "trading"
        self.state.entry_time = time.time()
        self.state.active_twap_algo_id = None
        self.state.active_twap_client_algo_id = None
        self.state.active_twap_started_at = 0.0
        self.state.active_twap_status = "spot_market_entry"
        self.state.active_twap_quote_budget = executed_quote
        self.state.active_twap_executed_quote = executed_quote
        self.state.position_qty = qty
        self.state.position_free_qty = qty if self.dry_run else Decimal("0")
        self.state.position_locked_qty = Decimal("0")
        self.state.position_cost_basis = executed_quote
        self.state.budget_remaining = max(Decimal("0"), self.state.budget_total - executed_quote)
        self.state.cost_basis_source = "market"
        self.state.entry_price = (executed_quote / qty).quantize(Decimal("0.00000001"))
        self.state.current_profit_usdt = Decimal("0")
        self.state.peak_profit_usdt = Decimal("0")
        self.state.last_exit_reason = ""
        self.state.manual_entry_override_until = 0.0
        self._clear_target_sell_order()
        if not self.dry_run:
            self._sync_position_snapshot()
        self._ensure_stop_loss_order(force=True)

    def _dca_order_client_prefix(self) -> str:
        return f"br-dca-{self.symbol.lower()}-"

    def _dca_step_pct(self) -> Decimal:
        volatility_step = Decimal(str(max(self.state.current_volatility, 0.0))) * Decimal("3")
        return max(self.state.adaptive_dca_min_step_pct, min(self.state.adaptive_dca_max_step_pct, volatility_step))

    def _open_dca_buy_orders(self) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        try:
            orders = self._send_signed_request("GET", "/api/v3/openOrders", {"symbol": self.symbol})
        except Exception as exc:
            LOGGER.warning("Unable to list DCA orders for %s: %s", self.symbol, exc)
            return []
        prefix = self._dca_order_client_prefix()
        return [
            order
            for order in orders
            if str(order.get("side") or "").upper() == "BUY"
            and str(order.get("clientOrderId") or "").startswith(prefix)
        ]

    def _cancel_dca_buy_orders(self) -> int:
        cancelled = 0
        for order in self._open_dca_buy_orders():
            order_id = order.get("orderId")
            if order_id in (None, ""):
                continue
            try:
                self._send_signed_request("DELETE", "/api/v3/order", {
                    "symbol": self.symbol,
                    "orderId": order_id,
                })
            except Exception as exc:
                LOGGER.warning("Unable to cancel DCA buy order %s for %s: %s", order_id, self.symbol, exc)
            else:
                cancelled += 1
        return cancelled

    @staticmethod
    def _minimum_quote_for_entry(price: Decimal, min_qty: Decimal, min_notional: Decimal) -> Decimal:
        qty_floor_quote = Decimal("0")
        if price > 0 and min_qty > 0:
            qty_floor_quote = min_qty * price
        minimum_quote = max(min_notional, qty_floor_quote)
        if minimum_quote <= 0:
            return Decimal("0")
        # Add a small buffer for rounding and tick-size effects.
        return (minimum_quote * Decimal("1.02")).quantize(Decimal("0.01"), rounding=ROUND_UP)

    def _start_single_spot_entry(self, quote_budget: Decimal, price: Decimal, min_qty: Decimal, min_notional: Decimal) -> tuple[bool, str]:
        min_required_quote = self._minimum_quote_for_entry(price, min_qty, min_notional)
        if min_required_quote > 0 and quote_budget < min_required_quote:
            return False, (
                f"Entry budget ${float(quote_budget):.2f} is below exchange minimum ${float(min_required_quote):.2f} "
                f"for {self.symbol}. Increase budget or use a cheaper pair."
            )

        if self.dry_run:
            quantity = self._round_quantity(quote_budget / price) if price > 0 else Decimal("0")
            executed_quote = (quantity * price).quantize(Decimal("0.01")) if quantity > 0 else Decimal("0")
            if quantity <= 0 or executed_quote <= 0:
                return False, "Manual fallback entry produced zero quantity in dry run"
        else:
            try:
                response = self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "type": "MARKET",
                    "quoteOrderQty": format(quote_budget, "f"),
                    "newOrderRespType": "FULL",
                })
            except Exception as exc:
                return False, f"Single-entry fallback failed: {str(exc)[:140]}"

            if isinstance(response, dict) and response.get("code") not in (None, 0, "0"):
                return False, f"Single-entry fallback rejected: {response}"

            quantity = self._decimal_from_fields(response, "executedQty", "origQty")
            executed_quote = self._decimal_from_fields(response, "cummulativeQuoteQty")
            if quantity <= 0 and price > 0:
                quantity = self._round_quantity(quote_budget / price)
            if executed_quote <= 0 and quantity > 0 and price > 0:
                executed_quote = (quantity * price).quantize(Decimal("0.01"))

            if quantity <= 0 or executed_quote <= 0:
                return False, "Single-entry fallback produced zero fill"

        self.state.status = "trading"
        self.state.entry_time = time.time()
        self.state.active_twap_algo_id = None
        self.state.active_twap_client_algo_id = None
        self.state.active_twap_started_at = time.time()
        self.state.active_twap_status = "spot_market_entry"
        self.state.active_twap_quote_budget = executed_quote
        self.state.active_twap_executed_quote = executed_quote
        self.state.position_qty = quantity
        self.state.position_free_qty = quantity if self.dry_run else Decimal("0")
        self.state.position_locked_qty = Decimal("0")
        self.state.position_cost_basis = executed_quote
        self.state.budget_remaining = max(Decimal("0"), self.state.budget_total - executed_quote)
        self.state.cost_basis_source = "market"
        self.state.entry_price = (executed_quote / quantity).quantize(Decimal("0.00000001")) if quantity > 0 else None
        self.state.current_profit_usdt = Decimal("0")
        self.state.peak_profit_usdt = Decimal("0")
        self.state.dca_order_count = 0
        self.state.last_exit_reason = "Single-entry fallback used because DCA ladder levels were below exchange minimum"
        self._clear_target_sell_order()
        if not self.dry_run:
            self._sync_position_snapshot()
        self._ensure_stop_loss_order(force=True)
        return True, self.state.last_exit_reason

    def _start_dca_buy_cycle(self, quote_budget: Decimal) -> None:
        if not self.dry_run:
            stock_block_reason = self._stock_trading_block_reason()
            if stock_block_reason:
                self.state.active_twap_status = "stock_api_unavailable"
                self.state.last_exit_reason = stock_block_reason
                return
        price = Decimal(str(self.state.current_price))
        if price <= 0:
            self.state.last_exit_reason = "DCA start skipped because price is unavailable"
            return
        if quote_budget <= 0:
            self.state.last_exit_reason = "DCA start skipped because budget is zero"
            return

        if not self.dry_run:
            free_quote = self._quote_asset_free_balance()
            quote_budget = min(quote_budget, (free_quote * Decimal("0.98")).quantize(Decimal("0.01")))
            if quote_budget <= 0:
                self.state.active_twap_status = "waiting_quote_balance"
                self.state.last_exit_reason = f"No available {self._quote_asset_for_symbol()} balance for DCA orders"
                return

        self._cancel_dca_buy_orders()
        step_pct = self._dca_step_pct()
        configured_levels = max(1, int(self.state.adaptive_dca_levels))
        step_size, min_qty, min_notional = self._get_symbol_trade_filters(self.symbol)
        min_required_quote = self._minimum_quote_for_entry(price, min_qty, min_notional)

        levels = configured_levels
        while levels > 1:
            quote_per_level_probe = (quote_budget / Decimal(str(levels))).quantize(Decimal("0.01"))
            level1_price = self._round_price_down(price * (Decimal("1") - step_pct))
            if level1_price <= 0:
                level1_price = price
            qty_probe = self._round_quantity_to_step(quote_per_level_probe / level1_price, step_size)
            notional_probe = qty_probe * level1_price
            qty_ok = qty_probe > 0 and qty_probe >= min_qty
            notional_ok = min_notional <= 0 or notional_probe >= min_notional
            if qty_ok and notional_ok:
                break
            levels -= 1

        quote_per_level = (quote_budget / Decimal(str(levels))).quantize(Decimal("0.01"))
        placed = 0
        skipped: list[str] = []

        for level in range(1, levels + 1):
            limit_price = self._round_price_down(price * (Decimal("1") - (step_pct * Decimal(str(level)))))
            if limit_price <= 0:
                skipped.append(f"level {level}: invalid price")
                continue
            quantity = self._round_quantity_to_step(quote_per_level / limit_price, step_size)
            notional = quantity * limit_price
            if quantity <= 0 or quantity < min_qty:
                skipped.append(f"level {level}: quantity below minimum")
                continue
            if min_notional > 0 and notional < min_notional:
                skipped.append(f"level {level}: notional ${float(notional):.2f} below minimum")
                continue

            if self.dry_run:
                placed += 1
                continue
            client_order_id = f"{self._dca_order_client_prefix()}{int(time.time())}-{level}"[:36]
            try:
                self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": format(quantity, "f"),
                    "price": format(limit_price, "f"),
                    "newClientOrderId": client_order_id,
                    "newOrderRespType": "FULL",
                })
            except Exception as exc:
                skipped.append(f"level {level}: {str(exc)[:120]}")
            else:
                placed += 1

        if placed <= 0:
            entered, fallback_reason = self._start_single_spot_entry(quote_budget, price, min_qty, min_notional)
            if entered:
                return
            self.state.active_twap_status = "dca_rejected"
            reason_parts = ["No DCA buy orders placed"]
            if min_required_quote > 0:
                reason_parts.append(
                    f"minimum entry for {self.symbol} is ~${float(min_required_quote):.2f}"
                )
            reason_parts.append(fallback_reason)
            if skipped:
                reason_parts.append("; ".join(skipped[:3]))
            self.state.last_exit_reason = ". ".join(reason_parts)
            return

        self.state.status = "trading"
        self.state.entry_time = time.time()
        self.state.active_twap_algo_id = None
        self.state.active_twap_client_algo_id = None
        self.state.active_twap_started_at = time.time()
        self.state.active_twap_status = "dca_buy_ladder"
        self.state.active_twap_quote_budget = quote_budget
        self.state.active_twap_executed_quote = Decimal("0")
        self.state.dca_order_count = placed
        self.state.dca_last_repriced_at = time.time()
        self.state.strategy_active = "dca_buy"
        reduction_note = ""
        if levels < configured_levels:
            reduction_note = f" (auto-reduced from {configured_levels} to {levels} levels for minimum order size)"
        self.state.strategy_note = (
            f"DCA buy ladder placed: {placed}/{levels} levels, step {float(step_pct * Decimal('100')):.2f}%"
            f"{reduction_note}"
        )
        self.state.last_exit_reason = self.state.strategy_note

    def _maintain_dca_buy_ladder(self) -> None:
        if self.state.active_twap_status != "dca_buy_ladder":
            return
        open_orders = self._open_dca_buy_orders()
        self.state.dca_order_count = len(open_orders)
        remaining_budget = max(Decimal("0"), self.state.active_twap_quote_budget - self.state.position_cost_basis)
        if self.state.position_qty > 0:
            self._ensure_stop_loss_order()
        if remaining_budget <= Decimal("0"):
            self.state.active_twap_status = "dca_filled"
            return
        if open_orders and time.time() - self.state.dca_last_repriced_at < self.state.adaptive_dca_reprice_interval_seconds:
            return
        if self.state.position_qty <= 0 or not open_orders:
            self._cancel_dca_buy_orders()
            self._start_dca_buy_cycle(remaining_budget)

    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        # When the bot is idle, the run loop isn't updating current_price.
        # Fetch a fresh price on-demand so the dashboard never shows $0.
        if not self.running and status.get("current_price", 0.0) == 0.0:
            try:
                fresh_price = self._get_price(timeout_seconds=5)
                if fresh_price > 0:
                    status["current_price"] = float(fresh_price)
                    with self.lock:
                        self.state.current_price = fresh_price
            except Exception:
                pass
        with self.lock:
            fresh_codex_status = self._check_codex_cli_status()
            if self.state.advisor_source and "copilot_cli=" in self.state.advisor_source:
                self.state.advisor_source = re.sub(r"copilot_cli=[^;]+", f"codex_cli={fresh_codex_status}", self.state.advisor_source)
            if self.state.advisor_source and "codex_cli=" in self.state.advisor_source:
                self.state.advisor_source = re.sub(r"codex_cli=[^;]+", f"codex_cli={fresh_codex_status}", self.state.advisor_source)
            if self.state.target_profit_suggestion_reason:
                self.state.target_profit_suggestion_reason = self.state.target_profit_suggestion_reason.replace("copilot=unavailable:FileNotFoundError", f"codex={fresh_codex_status}")
                self.state.target_profit_suggestion_reason = self.state.target_profit_suggestion_reason.replace("copilot=unavailable_install_required:FileNotFoundError", f"codex={fresh_codex_status}")
                self.state.target_profit_suggestion_reason = re.sub(r"\bcopilot=", "codex=", self.state.target_profit_suggestion_reason)
            if fresh_codex_status != self.state.codex_cli_status or fresh_codex_status == "available":
                self.state.codex_cli_status = fresh_codex_status
                self._save_state()
            status["codex_cli_status"] = fresh_codex_status
            status["advisor_source"] = self.state.advisor_source
            status["target_profit_suggestion_reason"] = self.state.target_profit_suggestion_reason
            budget_total = self.state.budget_total
            budget_remaining = self.state.budget_remaining
            # Live runs can carry historical configured budget values; clamp display to current account reality.
            if not self.dry_run and self.state.total_asset_value_usdt > Decimal("0"):
                if budget_total > self.state.total_asset_value_usdt * Decimal("2"):
                    budget_total = self.state.total_asset_value_usdt
                budget_remaining = max(Decimal("0"), budget_total - self.state.position_cost_basis)

            status.update({
                "strategy_name": "1h Spot TWAP Swing",
                "strategy_mode": self.state.strategy_mode,
                "strategy_active": self.state.strategy_active,
                "strategy_note": self.state.strategy_note,
                "twap_auto_switch_budget_usdt": float(self.state.twap_auto_switch_budget_usdt),
                "swing_cycle_budget_total": float(budget_total),
                "swing_cycle_budget_remaining": float(budget_remaining),
                "twap_status": self.state.active_twap_status,
                "twap_algo_id": self.state.active_twap_algo_id,
                "twap_client_algo_id": self.state.active_twap_client_algo_id,
                "twap_duration_seconds": self.state.twap_duration_seconds,
                "twap_quote_budget": float(self.state.active_twap_quote_budget),
                "twap_executed_quote": float(self.state.active_twap_executed_quote),
                "twap_rsi_start_threshold": self.state.twap_rsi_start_threshold,
                "dca_order_count": self.state.dca_order_count,
                "dca_last_repriced_at": self.state.dca_last_repriced_at,
                "dca_levels": self.dca_levels,
                "last_exit_reason": self.state.last_exit_reason,
                "can_start": self.state.status in ("idle", "completed", "error"),
                "can_stop": self.state.status in ("waiting", "trading"),
            })
        return status

    def start(self, budget_usdt: float = 5000, duration_hours: float = 12.0) -> str:
        with self.lock:
            if self.running:
                return "Bot is already running"
            requested_budget = Decimal(str(budget_usdt)).quantize(Decimal("0.01"))
            effective_budget = requested_budget
            if not self.dry_run:
                free_quote = self._quote_asset_free_balance()
                if free_quote > Decimal("0"):
                    effective_budget = min(requested_budget, free_quote)
                if effective_budget <= Decimal("0"):
                    raise ValueError(f"No available {self._quote_asset_for_symbol()} balance to start")

            self.state.budget_total = effective_budget
            self.state.budget_remaining = effective_budget
            self.state.duration_hours = 24 * 365 * 10
            self.state.started_at = time.time()
            self.state.status = "waiting"
            self.state.user_requested_start = True
            self.state.user_requested_stop = False
            self.state.strategy_active = "twap"
            if self.state.strategy_mode == "spot":
                self.state.strategy_note = "Manual spot market strategy selected"
            elif self.state.strategy_mode == "twap":
                self.state.strategy_note = "Manual TWAP strategy selected"
            else:
                self.state.strategy_note = "Auto strategy: use spot entry for small budgets, switch to TWAP when budget is sufficient"
            self._reset_cycle_state(keep_cycle_count=True)
            self._save_state()

            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            duration_hours = self.state.twap_duration_seconds / 3600.0
            return f"{duration_hours:g}h TWAP swing bot started (budget ${float(effective_budget):.2f})"

    def request_manual_start_override(self, window_seconds: int = 90, reset_daily_drawdown: bool = True) -> str:
        with self.lock:
            now = time.time()
            note_parts: list[str] = []
            if reset_daily_drawdown and self.state.daily_drawdown_triggered:
                self.state.daily_drawdown_day = time.strftime("%Y-%m-%d", time.gmtime(now))
                self.state.daily_equity_baseline_usdt = self._current_equity_usdt()
                self.state.daily_drawdown_pct = 0.0
                self.state.daily_drawdown_triggered = False
                note_parts.append("daily drawdown guard reset")

            self.state.user_requested_start = True
            self.state.user_requested_stop = False
            self.state.manual_entry_override_until = now + max(15, int(window_seconds))
            note_parts.append(f"manual entry override enabled for {max(15, int(window_seconds))}s")
            self._save_state()
            return "; ".join(note_parts)

    def rearm_running_start(self, budget_usdt: float = 5000, duration_hours: float = 12.0) -> str:
        with self.lock:
            requested_budget = Decimal(str(budget_usdt)).quantize(Decimal("0.01"))
            effective_budget = requested_budget
            if not self.dry_run:
                free_quote = self._quote_asset_free_balance()
                if free_quote > Decimal("0"):
                    effective_budget = min(requested_budget, free_quote)
                if effective_budget <= Decimal("0"):
                    raise ValueError(f"No available {self._quote_asset_for_symbol()} balance to start")

            self.state.budget_total = effective_budget
            if self.state.position_cost_basis > Decimal("0"):
                self.state.budget_remaining = max(Decimal("0"), effective_budget - self.state.position_cost_basis)
            else:
                self.state.budget_remaining = effective_budget
            self.state.duration_hours = max(12.0, float(duration_hours or 12.0))
            if not self.state.started_at:
                self.state.started_at = time.time()
            if self.state.status in {"idle", "completed", "error"}:
                self.state.status = "waiting"
            if (
                self.state.position_qty <= Decimal("0")
                and not self.state.active_twap_algo_id
                and not self.state.target_sell_order_id
                and not self.state.stop_loss_order_id
            ):
                self._reset_cycle_state(keep_cycle_count=True)
            self.state.user_requested_start = True
            self.state.user_requested_stop = False
            self._save_state()
            return f"start settings re-armed (budget ${float(effective_budget):.2f})"

    def resume(self) -> str:
        with self.lock:
            if self.running:
                return "Bot is already running"
            if self.state.status not in {"waiting", "trading"}:
                return self.start(float(self.state.budget_total), 12.0)
            self.state.user_requested_start = True
            self.state.user_requested_stop = False
            self.running = True
            self.thread = threading.Thread(target=self._run_loop, daemon=True)
            self.thread.start()
            self._save_state()
            return f"Resumed {self.symbol} TWAP swing bot"

    def restart(self) -> str:
        self.stop()
        time.sleep(0.5)
        with self.lock:
            self.state.status = "idle"
            self.state.user_requested_stop = False
            self._reset_cycle_state(keep_cycle_count=True)
            self._save_state()
        return "TWAP swing bot restarted"

    def _run_loop(self) -> None:
        try:
            while self.running:
                try:
                    self._step()
                    time.sleep(self.trading_poll_seconds if self.state.status == "trading" else self.waiting_poll_seconds)
                except Exception as exc:
                    LOGGER.error("TWAP swing bot error: %s", exc)
                    self._send_telegram_message(f"❌ *TWAP BOT ERROR*\n\n{str(exc)[:300]}")
                    with self.lock:
                        self.state.status = "error"
                        self._save_state()
                    time.sleep(60)
                    with self.lock:
                        if self.running and self.state.status == "error":
                            self.state.status = "waiting"
                            self._save_state()
        finally:
            self.running = False

    def _step(self) -> None:
        price = self._get_price()
        volatility, rsi = self._get_market_data()
        sentiment, sentiment_name = self._get_sentiment()

        with self.lock:
            self.state.current_price = price
            self.state.current_volatility = volatility
            self.state.current_rsi = rsi
            self.state.market_sentiment = sentiment
            self.state.sentiment_name = sentiment_name
            media_score, media_label, media_summary = self._get_media_sentiment()
            self.state.media_sentiment_score = media_score
            self.state.media_sentiment_label = media_label
            self.state.media_sentiment_summary = media_summary
            self.state.media_news_headlines = self._media_headlines_cache
            liquidation_signal = self._get_liquidation_signal(self.symbol)
            self.state.liquidation_pressure_score = float(liquidation_signal.get("score", 0.0) or 0.0)
            self.state.liquidation_bias = str(liquidation_signal.get("bias", "neutral"))
            self.state.liquidation_notional_usdt = float(liquidation_signal.get("notional_usdt", 0.0) or 0.0)
            self.state.liquidation_summary = str(liquidation_signal.get("summary", "No liquidation signal"))[:360]
            self.state.is_volatile = volatility >= self.state.volatility_threshold
            self._sync_position_snapshot()
            self._update_total_asset_snapshot()
            self._sync_daily_drawdown_guard()

            if self.state.status in {"idle", "completed", "error"} and not self.state.user_requested_stop:
                self.state.status = "waiting"

            self._update_advisor_signal()

            if (
                self.state.status == "trading"
                and (self.state.position_qty <= Decimal("0") or self.state.position_cost_basis < self.dust_position_cost_usdt)
                and not self.state.active_twap_algo_id
                and not self.state.target_sell_order_id
                and not self.state.stop_loss_order_id
            ):
                previous_reason = self.state.last_exit_reason
                self._reset_cycle_state(keep_cycle_count=True)
                self.state.status = "waiting"
                self.state.last_exit_reason = previous_reason or "Recovered closed position; waiting for next entry"
                LOGGER.info("Recovered %s from stale trading state with no position/TWAP", self.symbol)

            if self.state.status == "waiting":
                if self.state.position_qty > 0:
                    self.state.status = "trading"
                    if not self.state.active_twap_algo_id:
                        self.state.active_twap_status = "manual_position"
                elif self._should_start_twap(rsi):
                    strategy, strategy_note, quote_budget = self._resolve_entry_strategy()
                    self.state.strategy_active = strategy
                    self.state.strategy_note = strategy_note
                    if strategy == "twap":
                        self._start_twap_cycle()
                    else:
                        self._start_spot_cycle(quote_budget)
                else:
                    LOGGER.info("Waiting for RSI <= %.1f. Current RSI %.2f", self.state.twap_rsi_start_threshold, rsi)
            elif self.state.status == "trading":
                self._sync_active_twap_order()
                self._sync_position_snapshot()
                self._maintain_dca_buy_ladder()
                stop_loss_fill_reason = self._sync_stop_loss_order()
                exit_reason = self._exit_reason(rsi)
                target_fill_reason = self._sync_target_sell_order()
                if stop_loss_fill_reason:
                    self._cancel_active_twap()
                    self._cancel_target_sell_order()
                    self._sync_position_snapshot()
                    self._finish_cycle_and_restart(stop_loss_fill_reason)
                elif target_fill_reason:
                    self._cancel_active_twap()
                    self._cancel_stop_loss_order()
                    self._sync_position_snapshot()
                    if self.state.position_qty <= self._get_quantity_step_size():
                        self._finish_cycle_and_restart(target_fill_reason)
                    else:
                        self.state.last_exit_reason = f"{target_fill_reason}; residual {float(self.state.position_qty):.8f} {self._base_asset()} remains"
                        self.state.target_sell_last_checked_at = 0.0
                        self._ensure_stop_loss_order(force=True)
                        self._ensure_target_sell_order(force=True)
                elif exit_reason:
                    self._close_cycle(exit_reason)
                elif self._twap_expired():
                    self._handle_twap_expiry()
                else:
                    self._ensure_stop_loss_order()
                    self._ensure_target_sell_order()

            self._save_state()

    def _should_start_twap(self, rsi: float) -> bool:
        if not self.dry_run:
            stock_block_reason = self._stock_trading_block_reason()
            if stock_block_reason:
                self.state.active_twap_status = "stock_api_unavailable"
                self.state.last_exit_reason = stock_block_reason
                return False
        manual_override_active = self.state.manual_entry_override_until > time.time()
        if manual_override_active:
            self.state.last_exit_reason = "Manual start override active: bypassing rank, RSI, and daily drawdown filters"
            return self.state.budget_remaining > Decimal("0")
        if self.state.daily_drawdown_triggered:
            self.state.last_exit_reason = (
                f"Daily drawdown kill-switch active ({self.state.daily_drawdown_pct * 100.0:.2f}% >= "
                f"{float(self.state.daily_drawdown_limit_pct * Decimal('100')):.2f}%)."
            )
            return False
        if self.entry_guard:
            allowed, reason = self.entry_guard(self.symbol)
            if not allowed:
                self.state.last_exit_reason = reason
                return False
        if rsi >= self.strict_entry_rsi:
            self.state.last_exit_reason = f"Entry blocked: RSI {rsi:.2f} is not below {self.strict_entry_rsi:.0f}"
            return False
        return self.state.budget_remaining > Decimal("0")

    def _start_twap_cycle(self) -> None:
        manual_override_active = self.state.manual_entry_override_until > time.time()
        if self.state.current_rsi >= self.strict_entry_rsi and not manual_override_active:
            self.state.active_twap_status = "waiting_rsi"
            self.state.last_exit_reason = f"Entry blocked: RSI {self.state.current_rsi:.2f} is not below {self.strict_entry_rsi:.0f}"
            return
        price = Decimal(str(self.state.current_price))
        if price <= 0:
            LOGGER.info("Skipping TWAP start because price is unavailable")
            return

        quote_budget = self._twap_quote_budget()
        if quote_budget <= 0:
            self.state.last_exit_reason = "Risk sizing returned zero budget; skipping TWAP start"
            return
        if quote_budget < self.state.min_twap_quote_budget_usdt:
            self.state.active_twap_status = "waiting_min_budget"
            self.state.last_exit_reason = (
                f"TWAP budget ${float(quote_budget):.2f} is below Binance's minimum notional "
                f"${float(self.state.min_twap_quote_budget_usdt):.2f}. "
                f"Add more {self._quote_asset_for_symbol()} balance (need at least "
                f"${float(self.state.min_twap_quote_budget_usdt):.0f}) or lower position sizing."
            )
            LOGGER.info("TWAP skipped for %s: %s", self.symbol, self.state.last_exit_reason)
            return

        cycle_risk_usdt = (self._current_equity_usdt() * self.state.equity_risk_per_trade_pct).quantize(Decimal("0.01"))
        if cycle_risk_usdt > Decimal("0"):
            self.state.max_loss_usdt = self._cycle_max_loss_usdt()

        quantity = self._round_quantity(quote_budget / price)
        if quantity <= Decimal("0"):
            LOGGER.warning("Calculated TWAP quantity is zero for budget %s price %s", quote_budget, price)
            return

        if not self.dry_run:
            free_quote = self._quote_asset_free_balance()
            # Keep a small buffer for tiny fluctuations and rounding.
            required_quote = (quote_budget * Decimal("0.98")).quantize(Decimal("0.01"))
            if free_quote < required_quote:
                self.state.active_twap_status = "waiting_quote_balance"
                self.state.last_exit_reason = (
                    f"Insufficient {self._quote_asset_for_symbol()} free balance ({float(free_quote):.2f}) for "
                    f"TWAP budget {float(quote_budget):.2f}"
                )
                LOGGER.info("Skipping TWAP start for %s: %s", self.symbol, self.state.last_exit_reason)
                return

        if self.dry_run:
            response = {
                "algoId": f"DRY-{int(time.time())}",
                "clientAlgoId": None,
                "success": True,
                "status": "DRY_RUN",
            }
        else:
            try:
                response = self._send_signed_request("POST", "/sapi/v1/algo/spot/newOrderTwap", {
                    "symbol": self.symbol,
                    "side": "BUY",
                    "quantity": format(quantity, "f"),
                    "duration": self.state.twap_duration_seconds,
                })
            except Exception as exc:
                self.state.active_twap_status = "start_failed"
                self.state.last_exit_reason = f"TWAP start failed: {str(exc)[:180]}"
                LOGGER.warning("TWAP start skipped for %s: %s", self.symbol, exc)
                return
            if response.get("code") not in (None, 0, "0"):
                self.state.active_twap_status = "start_rejected"
                self.state.last_exit_reason = f"Binance TWAP rejected: {response}"
                LOGGER.warning("TWAP rejected for %s: %s", self.symbol, response)
                return

        self.state.status = "trading"
        self.state.entry_time = time.time()
        self.state.active_twap_started_at = time.time()
        algo_id = response.get("algoId")
        client_algo_id = response.get("clientAlgoId")
        self.state.active_twap_algo_id = str(algo_id or client_algo_id or "")
        self.state.active_twap_client_algo_id = str(client_algo_id or "") if client_algo_id else None
        self.state.active_twap_status = str(response.get("status") or "SUBMITTED")
        self.state.active_twap_quote_budget = quote_budget
        self.state.active_twap_executed_quote = Decimal("0")
        self.state.position_qty = Decimal("0")
        self.state.position_free_qty = Decimal("0")
        self.state.position_locked_qty = Decimal("0")
        self.state.position_cost_basis = Decimal("0")
        self.state.cost_basis_source = "twap"
        self.state.entry_price = None
        self.state.current_profit_usdt = Decimal("0")
        self.state.peak_profit_usdt = Decimal("0")
        self.state.last_exit_reason = ""
        self.state.manual_entry_override_until = 0.0
        self._clear_target_sell_order()
        LOGGER.info("Started 12h Spot TWAP %s qty=%s quote_budget=%s", self.state.active_twap_algo_id, quantity, quote_budget)
        self._send_telegram_message(
            f"🔁 *12H SPOT TWAP STARTED*\n\n"
            f"Symbol: {self.symbol}\n"
            f"Budget slice: ${float(quote_budget):.2f}\n"
            f"Quantity: {float(quantity):.8f} {self._base_asset()}\n"
            f"Risk per trade: {float(self.state.equity_risk_per_trade_pct * Decimal('100')):.2f}%\n"
            f"Daily DD guard: {float(self.state.daily_drawdown_limit_pct * Decimal('100')):.2f}%\n"
            f"RSI: {self.state.current_rsi:.2f}\n"
            f"Target: +${float(self.state.target_profit_usdt):.2f}\n"
            f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}"
        )

    def _quote_asset_free_balance(self) -> Decimal:
        if self.dry_run:
            return self.state.budget_remaining
        try:
            quote_asset = self._quote_asset_for_symbol()
            account = self._send_signed_request("GET", "/api/v3/account", {})
            balance = next((item for item in account.get("balances", []) if item.get("asset") == quote_asset), None)
            return Decimal(str(balance.get("free", "0"))) if balance else Decimal("0")
        except Exception as exc:
            LOGGER.warning("Unable to read %s free balance: %s", self._quote_asset_for_symbol(), exc)
            return Decimal("0")

    def _current_equity_usdt(self) -> Decimal:
        equity = self.state.total_asset_value_usdt
        if equity <= 0:
            equity = self.state.budget_remaining if self.state.budget_remaining > 0 else self.state.budget_total
        return max(equity, Decimal("0"))

    def _sync_daily_drawdown_guard(self) -> None:
        current_day = time.strftime("%Y-%m-%d", time.gmtime())
        equity = self._current_equity_usdt()
        if self.state.daily_drawdown_day != current_day or self.state.daily_equity_baseline_usdt <= 0:
            self.state.daily_drawdown_day = current_day
            self.state.daily_equity_baseline_usdt = equity
            self.state.daily_drawdown_pct = 0.0
            self.state.daily_drawdown_triggered = False
            return

        baseline = self.state.daily_equity_baseline_usdt
        if baseline <= 0:
            self.state.daily_drawdown_pct = 0.0
            return

        drawdown = (baseline - equity) / baseline
        if drawdown < 0:
            drawdown = Decimal("0")
        self.state.daily_drawdown_pct = float(drawdown)
        if drawdown >= self.state.daily_drawdown_limit_pct:
            if not self.state.daily_drawdown_triggered:
                limit_pct = float(self.state.daily_drawdown_limit_pct * Decimal("100"))
                hit_pct = float(drawdown * Decimal("100"))
                self.state.last_exit_reason = f"Daily drawdown kill-switch triggered at {hit_pct:.2f}% (limit {limit_pct:.2f}%)"
                self._send_telegram_message(
                    f"🛑 *DAILY DRAWDOWN KILL-SWITCH*\n\n"
                    f"Drawdown: {hit_pct:.2f}%\n"
                    f"Limit: {limit_pct:.2f}%\n"
                    f"New TWAP entries are paused until the next UTC day."
                )
            self.state.daily_drawdown_triggered = True

    def _sync_active_twap_order(self) -> None:
        if not self.state.active_twap_algo_id or self.dry_run:
            return
        try:
            algo_id = self._ensure_active_twap_algo_id()
            if not algo_id:
                LOGGER.warning("Unable to resolve numeric algoId for active TWAP %s", self.state.active_twap_algo_id)
                return
            result = self._send_signed_request("GET", "/sapi/v1/algo/spot/subOrders", {
                "algoId": algo_id,
                "page": 1,
                "pageSize": 100,
            })
            self.state.active_twap_status = str(result.get("algoStatus") or result.get("status") or self.state.active_twap_status)
            executed_quote = self._decimal_from_fields(result, "executedAmt", "executedQuoteQty", "cummulativeQuoteQty")
            executed_qty = self._decimal_from_fields(result, "executedQty", "executedQuantity", "cumQty")
            sub_orders = result.get("subOrders") or result.get("orders") or []
            if sub_orders:
                sub_executed_quote = Decimal("0")
                sub_executed_qty = Decimal("0")
                for order in sub_orders:
                    order_qty = self._decimal_from_fields(order, "executedQty", "executedQuantity", "cumQty")
                    order_quote = self._decimal_from_fields(order, "executedAmt", "executedQuoteQty", "cummulativeQuoteQty")
                    if order_quote <= 0 and order_qty > 0:
                        avg_price = self._decimal_from_fields(order, "avgPrice", "price")
                        if avg_price > 0:
                            order_quote = order_qty * avg_price
                    sub_executed_quote += order_quote
                    sub_executed_qty += order_qty
                if sub_executed_quote > 0 or sub_executed_qty > 0:
                    executed_quote = sub_executed_quote
                    executed_qty = sub_executed_qty
            if executed_quote > 0:
                self.state.active_twap_executed_quote = executed_quote
                self.state.position_cost_basis = executed_quote
            if executed_qty > 0:
                self.state.position_qty = executed_qty
        except Exception as exc:
            LOGGER.warning("Unable to sync TWAP sub-orders: %s", exc)

    def _sync_position_snapshot(self) -> None:
        if self.dry_run:
            self._update_position_from_twap_snapshot()
            return
        self._sync_account_position()
        self._update_position_from_twap_snapshot()

    def _sync_account_position(self) -> None:
        try:
            account = self._send_signed_request("GET", "/api/v3/account", {})
            base_asset = self._base_asset()
            balance = next((item for item in account.get("balances", []) if item.get("asset") == base_asset), None)
            free_qty = Decimal(str(balance.get("free", "0"))) if balance else Decimal("0")
            locked_qty = Decimal(str(balance.get("locked", "0"))) if balance else Decimal("0")
            total_qty = free_qty + locked_qty
            self.state.position_free_qty = free_qty
            self.state.position_locked_qty = locked_qty
            self.state.position_qty = total_qty

            if total_qty <= 0:
                self.state.position_cost_basis = Decimal("0")
                self.state.entry_price = None
                self.state.current_profit_usdt = Decimal("0")
                self.state.cost_basis_source = "none"
                return

            cost_basis, source = self._estimate_account_position_cost_basis(total_qty)
            if cost_basis <= 0 and self.state.position_cost_basis > 0:
                cost_basis = self.state.position_cost_basis
                source = "previous"
            if cost_basis <= 0:
                cost_basis = Decimal(str(self.state.current_price)) * total_qty
                source = "current_price_fallback"
            self.state.position_cost_basis = cost_basis
            self.state.cost_basis_source = source
        except Exception as exc:
            LOGGER.warning("Unable to sync %s account balance: %s", self._base_asset(), exc)

    def _estimate_account_position_cost_basis(self, account_qty: Decimal) -> tuple[Decimal, str]:
        try:
            trades = self._send_signed_request("GET", "/api/v3/myTrades", {
                "symbol": self.symbol,
                "limit": 1000,
            })
        except Exception as exc:
            LOGGER.warning("Unable to load %s trade history for cost basis: %s", self.symbol, exc)
            return Decimal("0"), "unavailable"

        inventory_qty = Decimal("0")
        inventory_cost = Decimal("0")
        base_asset = self._base_asset()
        quote_asset = self._quote_asset_for_symbol()
        for trade in sorted(trades, key=lambda item: int(item.get("time", 0))):
            qty = Decimal(str(trade.get("qty", "0")))
            quote_qty = Decimal(str(trade.get("quoteQty", "0")))
            commission = Decimal(str(trade.get("commission", "0")))
            commission_asset = str(trade.get("commissionAsset", ""))
            is_buy = bool(trade.get("isBuyer"))

            if is_buy:
                net_qty = qty - commission if commission_asset == base_asset else qty
                trade_cost = quote_qty + commission if commission_asset == quote_asset else quote_qty
                inventory_qty += max(net_qty, Decimal("0"))
                inventory_cost += trade_cost
            else:
                sold_qty = qty + commission if commission_asset == base_asset else qty
                if inventory_qty <= 0:
                    continue
                avg_cost = inventory_cost / inventory_qty
                inventory_qty -= sold_qty
                inventory_cost -= avg_cost * sold_qty
                if inventory_qty <= Decimal("0.0000000001"):
                    inventory_qty = Decimal("0")
                    inventory_cost = Decimal("0")

        if inventory_qty <= 0 or inventory_cost <= 0:
            return Decimal("0"), "unavailable"

        average_cost = inventory_cost / inventory_qty
        return average_cost * account_qty, "trade_history_estimate"

    def _ensure_active_twap_algo_id(self) -> str | None:
        current = str(self.state.active_twap_algo_id or "")
        if current.isdigit():
            return current

        client_algo_id = self.state.active_twap_client_algo_id or current
        for path, params in (
            ("/sapi/v1/algo/spot/openOrders", {}),
            ("/sapi/v1/algo/spot/historicalOrders", {"symbol": self.symbol, "side": "BUY", "page": 1, "pageSize": 100}),
        ):
            try:
                result = self._send_signed_request("GET", path, params)
            except Exception as exc:
                LOGGER.warning("Unable to resolve TWAP algoId from %s: %s", path, exc)
                continue
            for order in result.get("orders", []):
                order_client_id = str(order.get("clientAlgoId") or "")
                order_algo_id = str(order.get("algoId") or "")
                order_symbol = str(order.get("symbol") or "")
                order_side = str(order.get("side") or "")
                if client_algo_id and order_client_id == client_algo_id or (order_symbol == self.symbol and order_side == "BUY" and order_algo_id):
                    if order_algo_id:
                        self.state.active_twap_algo_id = order_algo_id
                        self.state.active_twap_client_algo_id = order_client_id or client_algo_id or None
                        self.state.active_twap_status = str(order.get("algoStatus") or order.get("status") or self.state.active_twap_status)
                        return order_algo_id
        return None

    @staticmethod
    def _decimal_from_fields(payload: dict[str, Any], *fields: str) -> Decimal:
        for field in fields:
            value = payload.get(field)
            if value not in (None, ""):
                try:
                    return Decimal(str(value))
                except Exception:
                    continue
        return Decimal("0")

    def _update_position_from_twap_snapshot(self) -> None:
        if self.dry_run:
            self._update_dry_run_position()
        if self.state.position_qty > 0 and self.state.position_cost_basis > 0:
            self.state.entry_price = self.state.position_cost_basis / self.state.position_qty
            pnl = (Decimal(str(self.state.current_price)) - self.state.entry_price) * self.state.position_qty
            self.state.current_profit_usdt = pnl
            if pnl > self.state.peak_profit_usdt:
                self.state.peak_profit_usdt = pnl

    def _update_dry_run_position(self) -> None:
        if self.state.status != "trading" or self.state.active_twap_quote_budget <= 0:
            return
        elapsed = max(0.0, time.time() - self.state.active_twap_started_at)
        progress = min(Decimal("1"), Decimal(str(elapsed)) / Decimal(str(self.state.twap_duration_seconds)))
        target_cost = (self.state.active_twap_quote_budget * progress).quantize(Decimal("0.00000001"))
        delta_cost = target_cost - self.state.position_cost_basis
        price = Decimal(str(self.state.current_price))
        if delta_cost > Decimal("0") and price > Decimal("0"):
            self.state.position_qty += delta_cost / price
            self.state.position_cost_basis += delta_cost
            self.state.active_twap_executed_quote = self.state.position_cost_basis
            self.state.last_twap_fill_at = time.time()

    def _exit_reason(self, rsi: float) -> str | None:
        if self.state.position_qty <= 0:
            return None
        if rsi >= 70 and self.state.current_profit_usdt > Decimal("0"):
            return f"RSI overbought at {rsi:.2f}; locking +${float(self.state.current_profit_usdt):.2f}"
        stop_loss = self._active_stop_loss_usdt()
        self.state.max_loss_usdt = stop_loss
        if self.state.current_profit_usdt <= -stop_loss:
            return f"Stop-loss hit: ${float(self.state.current_profit_usdt):.2f}"
        return None

    def _target_sell_order_open(self) -> bool:
        if not self.state.target_sell_order_id:
            return False
        return self.state.target_sell_status.upper() not in {
            "", "IDLE", "FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "PLACE_ERROR"
        }

    def _target_sell_limit_price(self) -> Decimal:
        if self.state.position_qty <= 0 or self.state.position_cost_basis <= 0:
            return Decimal("0")
        raw_price = (self.state.position_cost_basis + self.state.target_profit_usdt) / self.state.position_qty
        return self._round_price_up(raw_price)

    def _target_order_client_prefix(self) -> str:
        return f"twaptg_{self.symbol.lower()}_"

    def _reconcile_open_target_sell_orders(self) -> None:
        """Guarantee at most one open SELL LIMIT order for this pair."""
        if self.dry_run:
            return
        try:
            orders = self._send_signed_request("GET", "/api/v3/openOrders", {"symbol": self.symbol})
        except Exception as exc:
            LOGGER.warning("Unable to reconcile target sell orders for %s: %s", self.symbol, exc)
            return

        pair_sell_limits = [
            order for order in orders
            if str(order.get("side") or "").upper() == "SELL"
            and str(order.get("type") or "").upper() == "LIMIT"
        ]

        if not pair_sell_limits:
            if self.state.target_sell_order_id:
                self._clear_target_sell_order(status="idle")
            return

        keep_order = None
        tracked_order_id = str(self.state.target_sell_order_id or "")
        tracked_client_order_id = str(self.state.target_sell_client_order_id or "")
        if tracked_order_id:
            keep_order = next((o for o in pair_sell_limits if str(o.get("orderId") or "") == tracked_order_id), None)
        if keep_order is None and tracked_client_order_id:
            keep_order = next((o for o in pair_sell_limits if str(o.get("clientOrderId") or "") == tracked_client_order_id), None)
        if keep_order is None:
            keep_order = sorted(pair_sell_limits, key=lambda o: int(o.get("orderId") or 0))[0]

        keep_order_id = str(keep_order.get("orderId") or "")
        canceled_any = False
        for order in pair_sell_limits:
            order_id = str(order.get("orderId") or "")
            if not order_id or order_id == keep_order_id:
                continue
            try:
                self._send_signed_request("DELETE", "/api/v3/order", {
                    "symbol": self.symbol,
                    "orderId": order_id,
                })
                canceled_any = True
                LOGGER.info("Canceled duplicate target sell order %s for %s", order_id, self.symbol)
            except Exception as exc:
                LOGGER.warning("Unable to cancel duplicate target sell order %s for %s: %s", order_id, self.symbol, exc)

        self.state.target_sell_order_id = keep_order_id or None
        keep_client_id = str(keep_order.get("clientOrderId") or "")
        self.state.target_sell_client_order_id = keep_client_id or None
        self.state.target_sell_status = str(keep_order.get("status") or "NEW")
        keep_price = self._decimal_from_fields(keep_order, "price")
        keep_qty = self._decimal_from_fields(keep_order, "origQty", "quantity")
        if keep_price > 0:
            self.state.target_sell_price = keep_price
        if keep_qty > 0:
            self.state.target_sell_qty = keep_qty
        if canceled_any:
            self.state.target_sell_last_checked_at = 0.0

    def _sync_target_sell_order(self) -> str | None:
        order_id = self.state.target_sell_order_id
        if not order_id:
            return None

        if self.dry_run:
            if self.state.target_sell_price > 0 and Decimal(str(self.state.current_price)) >= self.state.target_sell_price:
                self.state.target_sell_status = "FILLED"
                self.state.current_profit_usdt = (self.state.target_sell_price * self.state.target_sell_qty) - self.state.position_cost_basis
                self.state.position_qty = Decimal("0")
                self.state.position_free_qty = Decimal("0")
                self.state.position_locked_qty = Decimal("0")
                return f"Target limit sell filled at ${float(self.state.target_sell_price):.2f}"
            return None

        try:
            order = self._send_signed_request("GET", "/api/v3/order", {
                "symbol": self.symbol,
                "orderId": order_id,
            })
        except Exception as exc:
            LOGGER.warning("Unable to sync target sell order %s: %s", order_id, exc)
            self.state.target_sell_status = "sync_error"
            return None

        status = str(order.get("status") or self.state.target_sell_status or "UNKNOWN")
        self.state.target_sell_status = status
        executed_qty = self._decimal_from_fields(order, "executedQty")
        order_qty = self._decimal_from_fields(order, "origQty", "quantity")
        order_price = self._decimal_from_fields(order, "price")
        if order_price > 0:
            self.state.target_sell_price = order_price
        if order_qty > 0:
            self.state.target_sell_qty = order_qty
        elif executed_qty > 0:
            self.state.target_sell_qty = executed_qty
        if status == "FILLED":
            return f"Target limit sell filled at ${float(self.state.target_sell_price):.2f}"
        if status in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            self._clear_target_sell_order(status=status)
        return None

    def _stop_loss_order_open(self) -> bool:
        if not self.state.stop_loss_order_id:
            return False
        return self.state.stop_loss_status.upper() not in {
            "FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "idle".upper()
        }

    def _stop_loss_client_prefix(self) -> str:
        return f"br-sl-{self.symbol.lower()}-"

    def _stop_loss_trigger_price(self) -> Decimal:
        if self.state.position_qty <= 0 or self.state.position_cost_basis <= 0:
            return Decimal("0")
        allowed_loss = min(self.max_order_loss_usdt, self._active_stop_loss_usdt())
        protected_value = self.state.position_cost_basis - allowed_loss
        if protected_value <= 0:
            return Decimal("0")
        return self._round_price_down(protected_value / self.state.position_qty)

    def _clear_stop_loss_order(self, status: str = "idle") -> None:
        self.state.stop_loss_order_id = None
        self.state.stop_loss_client_order_id = None
        self.state.stop_loss_status = status
        if status in {"idle", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH", "FILLED"}:
            self.state.stop_loss_price = Decimal("0")
            self.state.stop_loss_qty = Decimal("0")

    def _sync_stop_loss_order(self) -> str | None:
        order_id = self.state.stop_loss_order_id
        if not order_id:
            return None

        if self.dry_run:
            if self.state.stop_loss_price > 0 and Decimal(str(self.state.current_price)) <= self.state.stop_loss_price:
                self.state.stop_loss_status = "FILLED"
                self.state.current_profit_usdt = (self.state.stop_loss_price * self.state.stop_loss_qty) - self.state.position_cost_basis
                self.state.position_qty = Decimal("0")
                self.state.position_free_qty = Decimal("0")
                self.state.position_locked_qty = Decimal("0")
                return f"Protective stop-loss filled at ${float(self.state.stop_loss_price):.2f}"
            return None

        try:
            order = self._send_signed_request("GET", "/api/v3/order", {
                "symbol": self.symbol,
                "orderId": order_id,
            })
        except Exception as exc:
            LOGGER.warning("Unable to sync stop-loss order %s: %s", order_id, exc)
            self.state.stop_loss_status = "sync_error"
            return None

        status = str(order.get("status") or self.state.stop_loss_status or "UNKNOWN")
        self.state.stop_loss_status = status
        stop_price = self._decimal_from_fields(order, "stopPrice")
        order_qty = self._decimal_from_fields(order, "origQty", "quantity")
        executed_qty = self._decimal_from_fields(order, "executedQty")
        if stop_price > 0:
            self.state.stop_loss_price = stop_price
        if order_qty > 0:
            self.state.stop_loss_qty = order_qty
        elif executed_qty > 0:
            self.state.stop_loss_qty = executed_qty
        if status == "FILLED":
            return f"Protective stop-loss filled at ${float(self.state.stop_loss_price):.2f}"
        if status in {"CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            self._clear_stop_loss_order(status=status)
        return None

    def _ensure_stop_loss_order(self, force: bool = False) -> None:
        if self.state.position_qty <= 0 or self.state.position_cost_basis <= 0:
            self._clear_stop_loss_order()
            return

        now = time.time()
        stop_price = self._stop_loss_trigger_price()
        if stop_price <= 0:
            self.state.stop_loss_status = "waiting_for_cost_basis"
            return

        desired_qty = self._round_quantity(self.state.position_qty)
        if desired_qty <= 0:
            self.state.stop_loss_status = "waiting_for_quantity"
            self.state.stop_loss_last_checked_at = now
            return

        if self.is_stock_instrument():
            # Reality stock orders support market and limit only. Monitor the
            # stop in software rather than submitting a crypto STOP_LOSS order.
            self.state.stop_loss_price = stop_price
            self.state.stop_loss_qty = desired_qty
            self.state.stop_loss_last_checked_at = now
            if Decimal(str(self.state.current_price)) <= stop_price:
                block_reason = None if self.dry_run else self._stock_trading_block_reason()
                if block_reason:
                    self.state.stop_loss_status = "manual_stock_exit_required"
                    self.state.last_exit_reason = (
                        f"Stock software stop reached at ${float(self.state.current_price):.2f}. "
                        f"Sell or adjust the order manually in Bitget. {block_reason}"
                    )
                    return
                self.state.stop_loss_status = "triggering_market_exit"
                self._cancel_target_sell_order()
                self._place_sell_order()
                if self.state.status != "error":
                    self._finish_cycle_and_restart(f"Stock software stop market exit at ${float(self.state.current_price):.2f}")
                return
            self.state.stop_loss_status = "software_monitoring"
            if self.state.last_exit_reason.startswith("Protective stop-loss failed: Automated Reality stock orders"):
                self.state.last_exit_reason = (
                    f"Reality stock position protected by software stop monitoring at ${float(stop_price):.2f}; "
                    "the existing Bitget target sell remains unchanged."
                )
            return

        if Decimal(str(self.state.current_price)) <= stop_price:
            self.state.stop_loss_status = "triggering_market_exit"
            self._cancel_target_sell_order()
            self._place_sell_order()
            if self.state.status != "error":
                self._finish_cycle_and_restart(f"Protective stop-loss market exit at ${float(self.state.current_price):.2f}")
            return

        if not force and self._stop_loss_order_open() and now - self.state.stop_loss_last_checked_at < self.state.target_sell_interval_seconds:
            return

        same_price = self.state.stop_loss_price == stop_price
        same_qty = abs(self.state.stop_loss_qty - desired_qty) < self._get_quantity_step_size()
        if self._stop_loss_order_open() and same_price and same_qty:
            self.state.stop_loss_last_checked_at = now
            return

        # Cancel only the stop-loss; leave the TP order intact (they coexist)
        if self._stop_loss_order_open():
            self._cancel_stop_loss_order()

        qty_source = self.state.position_free_qty if self.state.position_free_qty > 0 else self.state.position_qty
        sell_qty = self._round_quantity(qty_source)
        if sell_qty <= 0:
            self.state.stop_loss_status = "waiting_for_free_balance"
            self.state.stop_loss_last_checked_at = now
            return
        self._place_stop_loss_order(sell_qty, stop_price)

    def _place_stop_loss_order(self, quantity: Decimal, stop_price: Decimal) -> None:
        self.state.stop_loss_last_checked_at = time.time()
        if self.dry_run:
            response = {
                "orderId": f"DRY-STOP-SELL-{int(time.time())}",
                "clientOrderId": f"{self._stop_loss_client_prefix()}{int(time.time())}",
                "status": "NEW",
            }
        else:
            client_order_id = f"{self._stop_loss_client_prefix()}{int(time.time())}"[:36]
            try:
                response = self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "SELL",
                    "type": "STOP_LOSS",
                    "quantity": format(quantity, "f"),
                    "stopPrice": format(stop_price, "f"),
                    "newClientOrderId": client_order_id,
                    "newOrderRespType": "FULL",
                })
            except Exception as exc:
                LOGGER.error("Protective stop-loss order failed: %s", exc)
                self.state.stop_loss_status = "place_error"
                self.state.last_exit_reason = f"Protective stop-loss failed: {str(exc)[:180]}"
                self._send_telegram_message(f"❌ *PROTECTIVE STOP-LOSS FAILED*\n\n{str(exc)[:300]}")
                return

        self.state.stop_loss_order_id = str(response.get("orderId") or "")
        self.state.stop_loss_client_order_id = str(response.get("clientOrderId") or "") or None
        self.state.stop_loss_status = str(response.get("status") or "NEW")
        self.state.stop_loss_price = stop_price
        self.state.stop_loss_qty = quantity
        LOGGER.info("Placed protective stop-loss %s qty=%s stop=%s", self.state.stop_loss_order_id, quantity, stop_price)
        self._send_telegram_message(
            f"🛡️ *PROTECTIVE STOP-LOSS PLACED*\n\n"
            f"Symbol: {self.symbol}\n"
            f"Quantity: {float(quantity):.8f} {self._base_asset()}\n"
            f"Stop price: ${float(stop_price):.2f}\n"
            f"Max planned loss: ${float(self.max_order_loss_usdt):.2f}\n"
            f"Order: {self.state.stop_loss_order_id}"
        )

    def _cancel_stop_loss_order(self) -> None:
        order_id = self.state.stop_loss_order_id
        if not order_id:
            return
        if self.dry_run:
            self._clear_stop_loss_order(status="CANCELED")
            return
        try:
            self._send_signed_request("DELETE", "/api/v3/order", {
                "symbol": self.symbol,
                "orderId": order_id,
            })
            self._clear_stop_loss_order(status="CANCELED")
        except Exception as exc:
            LOGGER.warning("Unable to cancel stop-loss order %s: %s", order_id, exc)
            self.state.stop_loss_status = "cancel_error"

    def _ratchet_target_profit_if_needed(self) -> None:
        """Move the take-profit UP when the current price exceeds the existing TP price.

        When the market has already moved past the limit sell price (e.g. the
        order hasn't been matched yet due to thin liquidity), we raise the target
        so the new limit order sits above the current price and lets the trade
        run further.  The target never moves DOWN (ratchet)."""
        if self.state.position_qty <= 0 or self.state.position_cost_basis <= 0:
            return
        current_price = Decimal(str(self.state.current_price))
        tp_price = self.state.target_sell_price
        if tp_price <= 0:
            tp_price = self._target_sell_limit_price()
        if tp_price <= 0:
            return
        if current_price <= tp_price:
            return  # price hasn't exceeded TP yet — nothing to ratchet

        # Price is ABOVE the TP: move TP to current_price + one target_pct step
        target_pct = (
            self.state.target_profit_usdt / self.state.position_cost_basis
            if self.state.position_cost_basis > 0
            else Decimal("0.006")
        )
        target_pct = max(Decimal("0.003"), min(target_pct, Decimal("0.035")))
        new_tp_price = self._round_price_up(current_price * (Decimal("1") + target_pct))
        new_target_profit = (new_tp_price * self.state.position_qty) - self.state.position_cost_basis
        new_target_profit = new_target_profit.quantize(Decimal("0.01"))

        if new_target_profit > self.state.target_profit_usdt:
            old = self.state.target_profit_usdt
            self.state.target_profit_usdt = new_target_profit
            self.state.target_sell_last_checked_at = 0.0  # force re-placement
            LOGGER.info(
                "TP ratcheted up: $%.2f -> $%.2f (price $%.2f exceeded old TP $%.2f)",
                float(old), float(new_target_profit),
                float(current_price), float(tp_price),
            )

    def _ensure_target_sell_order(self, force: bool = False) -> None:
        if self.state.position_qty <= 0:
            self._clear_target_sell_order()
            return

        self._reconcile_open_target_sell_orders()

        if not self.dry_run and self.is_stock_instrument():
            stock_block_reason = self._stock_trading_block_reason()
            if stock_block_reason:
                self.state.target_sell_last_checked_at = time.time()
                if self._target_sell_order_open():
                    # Preserve a manually-created Reality sell order. A Classic
                    # API key cannot replace it after cancellation.
                    return
                self.state.target_sell_status = "manual_stock_exit_required"
                self.state.last_exit_reason = stock_block_reason
                return

        # Ratchet only when the replacement order endpoint is available.
        self._ratchet_target_profit_if_needed()

        now = time.time()
        if not force and self._target_sell_order_open() and now - self.state.target_sell_last_checked_at < self.state.target_sell_interval_seconds:
            return

        target_price = self._target_sell_limit_price()
        if target_price <= 0:
            self.state.target_sell_status = "waiting_for_cost_basis"
            return

        desired_qty = self._round_quantity(self.state.position_qty)
        if desired_qty <= 0:
            self.state.target_sell_status = "waiting_for_quantity"
            self.state.target_sell_last_checked_at = now
            return

        had_open_order = self._target_sell_order_open()
        if had_open_order:
            same_price = self.state.target_sell_price == target_price
            same_qty = abs(self.state.target_sell_qty - desired_qty) < self._get_quantity_step_size()
            if same_price and same_qty:
                self.state.target_sell_last_checked_at = now
                return
            self._cancel_target_sell_order()

        qty_source = self.state.position_qty if had_open_order else self.state.position_free_qty
        if qty_source <= 0:
            self.state.target_sell_status = "waiting_for_free_balance"
            self.state.target_sell_last_checked_at = now
            return

        sell_qty = self._round_quantity(qty_source)
        if sell_qty <= 0:
            self.state.target_sell_status = "waiting_for_quantity"
            self.state.target_sell_last_checked_at = now
            return

        self._place_target_sell_order(sell_qty, target_price)

    def _place_target_sell_order(self, quantity: Decimal, price: Decimal) -> None:
        self.state.target_sell_last_checked_at = time.time()
        if self.dry_run:
            response = {
                "orderId": f"DRY-LIMIT-SELL-{int(time.time())}",
                "clientOrderId": f"{self._target_order_client_prefix()}{int(time.time())}",
                "status": "NEW",
            }
        else:
            try:
                client_order_id = f"{self._target_order_client_prefix()}{int(time.time())}"[:36]
                response = self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": self.symbol,
                    "side": "SELL",
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": format(quantity, "f"),
                    "price": format(price, "f"),
                    "newClientOrderId": client_order_id,
                    "newOrderRespType": "FULL",
                })
            except Exception as exc:
                LOGGER.error("Target limit sell order failed: %s", exc)
                self.state.target_sell_status = "place_error"
                self._send_telegram_message(f"❌ *TARGET LIMIT SELL FAILED*\n\n{str(exc)[:300]}")
                return

        self.state.target_sell_order_id = str(response.get("orderId") or "")
        self.state.target_sell_client_order_id = str(response.get("clientOrderId") or "") or None
        self.state.target_sell_status = str(response.get("status") or "NEW")
        self.state.target_sell_price = price
        self.state.target_sell_qty = quantity
        LOGGER.info("Placed target limit sell %s qty=%s price=%s", self.state.target_sell_order_id, quantity, price)
        self._send_telegram_message(
            f"🎯 *TARGET LIMIT SELL PLACED*\n\n"
            f"Symbol: {self.symbol}\n"
            f"Quantity: {float(quantity):.8f} {self._base_asset()}\n"
            f"Limit price: ${float(price):.2f}\n"
            f"Target profit: +${float(self.state.target_profit_usdt):.2f}\n"
            f"Order: {self.state.target_sell_order_id}"
        )

    def _cancel_target_sell_order(self) -> None:
        order_id = self.state.target_sell_order_id
        if not order_id:
            return
        if self.dry_run:
            self._clear_target_sell_order(status="CANCELED")
            return
        try:
            self._send_signed_request("DELETE", "/api/v3/order", {
                "symbol": self.symbol,
                "orderId": order_id,
            })
            self._clear_target_sell_order(status="CANCELED")
        except Exception as exc:
            LOGGER.warning("Unable to cancel target sell order %s: %s", order_id, exc)
            self.state.target_sell_status = "cancel_error"

    def _clear_target_sell_order(self, status: str = "idle") -> None:
        self.state.target_sell_order_id = None
        self.state.target_sell_client_order_id = None
        self.state.target_sell_status = status
        if status in {"idle", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}:
            self.state.target_sell_price = Decimal("0")
            self.state.target_sell_qty = Decimal("0")

    def _close_cycle(self, reason: str) -> None:
        if not self.dry_run and self.is_stock_instrument():
            stock_block_reason = self._stock_trading_block_reason()
            if stock_block_reason:
                self.state.last_exit_reason = (
                    f"{reason}. Automated stock exit was not attempted; manage the open Reality order "
                    f"manually in Bitget. {stock_block_reason}"
                )
                self.state.target_sell_last_checked_at = time.time()
                return
        self._cancel_target_sell_order()
        self._cancel_stop_loss_order()
        self._cancel_active_twap()
        if self.state.position_qty > 0:
            self._place_sell_order()
            if self.state.status == "error":
                return
        self._finish_cycle_and_restart(reason)

    def _handle_twap_expiry(self) -> None:
        self._cancel_active_twap()
        if self.state.position_qty > 0:
            self.state.active_twap_algo_id = None
            self.state.active_twap_status = "holding"
            self.state.active_twap_started_at = 0.0
            self.state.last_exit_reason = "12h TWAP window ended; holding for profit/RSI exit"
            self.state.target_sell_last_checked_at = 0.0
            self._ensure_stop_loss_order(force=True)
            self._ensure_target_sell_order(force=True)
            self._send_telegram_message(
                f"⏰ *12H TWAP WINDOW ENDED*\n\n"
                f"Filled: ${float(self.state.active_twap_executed_quote):.2f}\n"
                f"Current P&L: ${float(self.state.current_profit_usdt):.2f}\n"
                f"Holding until +${float(self.state.target_profit_usdt):.2f}, RSI overbought profit, or stop-loss."
            )
            return
        self._finish_cycle_and_restart("12h TWAP window ended with no fill")

    def close_all_positions_to_usdt(self) -> str:
        with self.lock:
            if not self.dry_run and self.is_stock_instrument() and (self.state.position_qty > 0 or self.state.target_sell_order_id):
                stock_block_reason = self._stock_trading_block_reason()
                if stock_block_reason:
                    return (
                        "Close-all was not submitted because the selected Reality stock has an open position/order "
                        f"that this Classic API key cannot safely replace. {stock_block_reason}"
                    )
            reason = "Manual close all positions to USDT"
            self._cancel_target_sell_order()
            self._cancel_stop_loss_order()
            self._cancel_active_twap()
            cancelled_algos, algo_cancel_failures = self._cancel_open_spot_algo_orders()
            cancelled_orders, cancel_failures = self._cancel_open_spot_orders()
            sold_assets, skipped_assets, sell_failures = self._sell_free_balances_to_usdt()

            self.state.active_twap_algo_id = None
            self.state.active_twap_client_algo_id = None
            self.state.active_twap_status = "idle"
            self.state.active_twap_started_at = 0.0
            self.state.active_twap_quote_budget = Decimal("0")
            self.state.active_twap_executed_quote = Decimal("0")
            self._clear_target_sell_order()
            self._clear_stop_loss_order()
            self.state.last_exit_reason = reason
            self.state.budget_remaining = self.state.budget_total
            self._sync_position_snapshot()
            self._update_total_asset_snapshot()
            self.state.status = "waiting" if self.running and not self.state.user_requested_stop else "idle"
            self._save_state()

        parts = [f"Close-all submitted. Sold {len(sold_assets)} asset(s) to USDT"]
        if cancelled_algos:
            parts.append(f"cancelled {cancelled_algos} open TWAP/algo order(s)")
        if cancelled_orders:
            parts.append(f"cancelled {cancelled_orders} open spot order(s)")
        if sold_assets:
            parts.append("sold: " + ", ".join(sold_assets))
        if skipped_assets:
            parts.append("skipped: " + "; ".join(skipped_assets))
        if algo_cancel_failures or cancel_failures or sell_failures:
            parts.append("failures: " + "; ".join(algo_cancel_failures + cancel_failures + sell_failures))
        message = ". ".join(parts) + "."
        LOGGER.info(message)
        self._send_telegram_message(f"🧯 *CLOSE ALL TO USDT*\n\n{message[:800]}")
        return message

    def close_pair_position_to_quote(self) -> str:
        with self.lock:
            base_asset = self._base_asset()
            quote_asset = self._quote_asset_for_symbol()
            reason = f"Manual close {self.symbol} orders and sell {base_asset} to {quote_asset}"
            self._cancel_target_sell_order()
            self._cancel_stop_loss_order()
            self._cancel_active_twap()
            cancelled_algos, algo_cancel_failures = self._cancel_open_spot_algo_orders(symbol_filter=self.symbol)
            cancelled_orders, cancel_failures = self._cancel_open_spot_orders(symbol_filter=self.symbol)
            sold_assets, skipped_assets, sell_failures = self._sell_base_asset_to_quote()

            self.state.active_twap_algo_id = None
            self.state.active_twap_client_algo_id = None
            self.state.active_twap_status = "idle"
            self.state.active_twap_started_at = 0.0
            self.state.active_twap_quote_budget = Decimal("0")
            self.state.active_twap_executed_quote = Decimal("0")
            self._clear_target_sell_order()
            self._clear_stop_loss_order()
            self.state.last_exit_reason = reason
            self._sync_position_snapshot()
            self._update_total_asset_snapshot()
            self.state.status = "waiting" if self.running and not self.state.user_requested_stop else "idle"
            self._save_state()

        parts = [f"Close {self.symbol} submitted"]
        if cancelled_algos:
            parts.append(f"cancelled {cancelled_algos} TWAP/algo order(s)")
        if cancelled_orders:
            parts.append(f"cancelled {cancelled_orders} spot order(s)")
        if sold_assets:
            parts.append("sold: " + ", ".join(sold_assets))
        if skipped_assets:
            parts.append("skipped: " + "; ".join(skipped_assets))
        if algo_cancel_failures or cancel_failures or sell_failures:
            parts.append("failures: " + "; ".join(algo_cancel_failures + cancel_failures + sell_failures))
        message = ". ".join(parts) + "."
        LOGGER.info(message)
        self._send_telegram_message(f"🧯 *CLOSE {self.symbol}*\n\n{message[:800]}")
        return message

    def _cancel_open_spot_algo_orders(self, symbol_filter: str | None = None) -> tuple[int, list[str]]:
        if self.dry_run or self.primary_exchange == "bitget":
            return 0, []
        try:
            result = self._send_signed_request("GET", "/sapi/v1/algo/spot/openOrders", {})
        except Exception as exc:
            LOGGER.warning("Unable to list open spot algo orders before close-all: %s", exc)
            return 0, [f"spot algo lookup: {exc}"]

        cancelled = 0
        failures: list[str] = []
        for order in result.get("orders", []):
            algo_id = order.get("algoId")
            symbol = str(order.get("symbol") or "")
            if symbol_filter and symbol != symbol_filter:
                continue
            if algo_id in (None, ""):
                continue
            try:
                self._send_signed_request("DELETE", "/sapi/v1/algo/spot/order", {"algoId": algo_id})
            except Exception as exc:
                failures.append(f"{symbol or 'algo'} {algo_id}: {exc}")
            else:
                cancelled += 1
        return cancelled, failures

    def _cancel_open_spot_orders(self, symbol_filter: str | None = None) -> tuple[int, list[str]]:
        if self.dry_run:
            return 0, []
        try:
            orders = self._send_signed_request("GET", "/api/v3/openOrders", {})
        except Exception as exc:
            LOGGER.warning("Unable to list open spot orders before close-all: %s", exc)
            return 0, [f"open order lookup: {exc}"]

        cancelled = 0
        failures: list[str] = []
        for order in orders:
            symbol = str(order.get("symbol") or "")
            order_id = order.get("orderId")
            if symbol_filter and symbol != symbol_filter:
                continue
            if not symbol or order_id in (None, ""):
                continue
            try:
                self._send_signed_request("DELETE", "/api/v3/order", {
                    "symbol": symbol,
                    "orderId": order_id,
                })
            except Exception as exc:
                failures.append(f"{symbol} order {order_id}: {exc}")
            else:
                cancelled += 1
        return cancelled, failures

    def _sell_base_asset_to_quote(self) -> tuple[list[str], list[str], list[str]]:
        base_asset = self._base_asset()
        quote_asset = self._quote_asset_for_symbol()
        if self.dry_run:
            sold = []
            if self.state.position_qty > 0:
                sold.append(f"{base_asset} {float(self.state.position_qty):.8f}")
            self.state.position_qty = Decimal("0")
            self.state.position_free_qty = Decimal("0")
            self.state.position_locked_qty = Decimal("0")
            self.state.position_cost_basis = Decimal("0")
            self.state.cost_basis_source = "none"
            self.state.entry_price = None
            self.state.current_profit_usdt = Decimal("0")
            return sold, [], []

        try:
            account = self._send_signed_request("GET", "/api/v3/account", {})
        except Exception as exc:
            return [], [], [f"account lookup: {exc}"]

        free_qty = Decimal("0")
        for balance in account.get("balances", []):
            if str(balance.get("asset") or "").upper() == base_asset:
                free_qty = Decimal(str(balance.get("free", "0")))
                break
        if free_qty <= 0:
            return [], [f"{base_asset}: no free balance to sell"], []

        symbol = self.symbol if self.symbol.endswith(quote_asset) else self._find_direct_quote_symbol(base_asset)
        if not symbol:
            return [], [f"{base_asset}: no direct {quote_asset} pair"], []

        try:
            step_size, min_qty, min_notional = self._get_symbol_trade_filters(symbol)
            sell_qty = self._round_quantity_to_step(free_qty, step_size)
            if sell_qty <= 0 or sell_qty < min_qty:
                return [], [f"{base_asset}: below minimum quantity"], []
            price = self._get_price(symbol)
            if min_notional > 0 and sell_qty * price < min_notional:
                return [], [f"{base_asset}: below minimum notional"], []
            self._send_signed_request("POST", "/api/v3/order", {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": format(sell_qty, "f"),
                "newOrderRespType": "FULL",
            })
        except Exception as exc:
            return [], [], [f"{base_asset}: {exc}"]
        return [f"{base_asset} {format(sell_qty, 'f')} to {quote_asset}"], [], []

    def _sell_free_balances_to_usdt(self) -> tuple[list[str], list[str], list[str]]:
        if self.dry_run:
            sold = []
            if self.state.position_qty > 0:
                sold.append(f"{self._base_asset()} {float(self.state.position_qty):.8f}")
            self.state.position_qty = Decimal("0")
            self.state.position_free_qty = Decimal("0")
            self.state.position_locked_qty = Decimal("0")
            self.state.position_cost_basis = Decimal("0")
            self.state.cost_basis_source = "none"
            self.state.entry_price = None
            self.state.current_profit_usdt = Decimal("0")
            return sold, [], []

        try:
            account = self._send_signed_request("GET", "/api/v3/account", {})
        except Exception as exc:
            return [], [], [f"account lookup: {exc}"]

        sold_assets: list[str] = []
        skipped_assets: list[str] = []
        failures: list[str] = []
        for balance in account.get("balances", []):
            asset = str(balance.get("asset") or "").upper()
            quote_asset = self._quote_asset_for_symbol()
            if not asset or asset == quote_asset:
                continue
            free_qty = Decimal(str(balance.get("free", "0")))
            if free_qty <= 0:
                continue

            symbol = self._find_direct_quote_symbol(asset)
            if not symbol:
                skipped_assets.append(f"{asset}: no direct {quote_asset} pair")
                continue

            try:
                step_size, min_qty, min_notional = self._get_symbol_trade_filters(symbol)
                sell_qty = self._round_quantity_to_step(free_qty, step_size)
                if sell_qty <= 0 or sell_qty < min_qty:
                    skipped_assets.append(f"{asset}: below minimum quantity")
                    continue
                price = self._get_price(symbol)
                if min_notional > 0 and sell_qty * price < min_notional:
                    skipped_assets.append(f"{asset}: below minimum notional")
                    continue
                self._send_signed_request("POST", "/api/v3/order", {
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "MARKET",
                    "quantity": format(sell_qty, "f"),
                    "newOrderRespType": "FULL",
                })
            except Exception as exc:
                failures.append(f"{asset}: {exc}")
            else:
                sold_assets.append(f"{asset} {format(sell_qty, 'f')}")
        return sold_assets, skipped_assets, failures

    def _update_total_asset_snapshot(self) -> None:
        if self.dry_run:
            price = Decimal(str(self.state.current_price or 0))
            position_value = self.state.position_qty * price if price > 0 else Decimal("0")
            total = self.state.budget_remaining + position_value
            self.state.total_asset_value_usdt = total
            self.state.total_asset_count = 1 if self.state.position_qty > 0 else 0
            self.state.total_asset_summary = f"DRY RUN: USDT ${float(self.state.budget_remaining):.2f}, {self._base_asset()} ${float(position_value):.2f}"
            self.state.total_asset_updated_at = time.time()
            return

        try:
            account = self._send_signed_request("GET", "/api/v3/account", {})
        except Exception as exc:
            LOGGER.warning("Unable to load total asset snapshot: %s", exc)
            self.state.total_asset_summary = f"Unavailable: {str(exc)[:120]}"
            self.state.total_asset_updated_at = time.time()
            return

        total_value = Decimal("0")
        asset_values: list[tuple[str, Decimal]] = []
        for balance in account.get("balances", []):
            asset = str(balance.get("asset") or "").upper()
            if not asset:
                continue
            quantity = Decimal(str(balance.get("free", "0"))) + Decimal(str(balance.get("locked", "0")))
            if quantity <= 0:
                continue
            quote_asset = self._quote_asset_for_symbol()
            if asset == quote_asset:
                value = quantity
            else:
                symbol = self._find_direct_quote_symbol(asset)
                if not symbol:
                    continue
                try:
                    value = quantity * self._get_price(symbol)
                except Exception as exc:
                    LOGGER.warning("Unable to price %s for total asset snapshot: %s", asset, exc)
                    continue
            if value <= 0:
                continue
            total_value += value
            asset_values.append((asset, value))

        asset_values.sort(key=lambda item: item[1], reverse=True)
        summary_parts = [f"{asset} ${float(value):.2f}" for asset, value in asset_values[:4]]
        if len(asset_values) > 4:
            summary_parts.append(f"+{len(asset_values) - 4} more")
        self.state.total_asset_value_usdt = total_value
        self.state.total_asset_count = len(asset_values)
        self.state.total_asset_summary = ", ".join(summary_parts) if summary_parts else "No priced spot assets"
        self.state.total_asset_updated_at = time.time()

    def _find_direct_quote_symbol(self, asset: str) -> str | None:
        quote_asset = self._quote_asset_for_symbol()
        candidate = f"{asset}{quote_asset}"
        try:
            if self.primary_exchange == "bitget":
                item = self._get_bitget_symbol_info(candidate)
                return candidate if str(item.get("status") or "").lower() == "online" else None
            query = urllib.parse.urlencode({"symbol": candidate})
            url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo?{query}"
            request = urllib.request.Request(url, headers={"User-Agent": "twap-swing-bot"})
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.loads(response.read().decode("utf-8"))
            symbols = payload.get("symbols") or []
            if symbols and str(symbols[0].get("status") or "") == "TRADING":
                return candidate
        except Exception:
            return None
        return None

    def _get_symbol_trade_filters(self, symbol: str) -> tuple[Decimal, Decimal, Decimal]:
        if self.primary_exchange == "bitget":
            item = self._get_bitget_symbol_info(symbol)
            step = Decimal("1") / (Decimal("10") ** int(item.get("quantityPrecision", 6)))
            minimum = Decimal(str(item.get("minTradeAmount") or "0"))
            min_notional = Decimal(str(item.get("minTradeUSDT") or "0"))
            return step, minimum, min_notional
        query = urllib.parse.urlencode({"symbol": symbol})
        url = f"{BINANCE_BASE_URL}/api/v3/exchangeInfo?{query}"
        request = urllib.request.Request(url, headers={"User-Agent": "twap-swing-bot"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        symbols = payload.get("symbols") or []
        if not symbols:
            raise RuntimeError(f"No exchange filters returned for {symbol}")
        filters = {item.get("filterType"): item for item in symbols[0].get("filters", [])}
        lot_filter = filters.get("LOT_SIZE", {})
        notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return (
            Decimal(str(lot_filter.get("stepSize", "0.00000001"))),
            Decimal(str(lot_filter.get("minQty", "0.00000001"))),
            Decimal(str(notional_filter.get("minNotional", "0"))),
        )

    @staticmethod
    def _round_quantity_to_step(quantity: Decimal, step_size: Decimal) -> Decimal:
        if step_size <= 0:
            return quantity
        steps = (quantity / step_size).to_integral_value(rounding=ROUND_DOWN)
        return steps * step_size

    def _finish_cycle_and_restart(self, reason: str) -> None:
        self.state.cycles_completed += 1
        self.state.last_exit_reason = reason
        LOGGER.info("TWAP swing cycle finished: %s", reason)
        self._send_telegram_message(
            f"✅ *TWAP SWING CYCLE CLOSED*\n\n"
            f"Reason: {reason}\n"
            f"P&L: ${float(self.state.current_profit_usdt):.2f}\n"
            f"Cycles: {self.state.cycles_completed}\n"
            f"Restarting next 12h TWAP watch."
        )
        self._reset_cycle_state(keep_cycle_count=True)
        self.state.status = "waiting"

    def _cancel_active_twap(self) -> None:
        algo_id = self.state.active_twap_algo_id
        if not algo_id:
            return
        if self.primary_exchange == "bitget":
            self.state.active_twap_status = "CANCELLED"
            self.state.active_twap_algo_id = None
            return
        if self.dry_run:
            self.state.active_twap_status = "CANCELLED"
            return
        try:
            self._send_signed_request("DELETE", "/sapi/v1/algo/spot/order", {"algoId": algo_id})
            self.state.active_twap_status = "CANCELLED"
        except Exception as exc:
            LOGGER.warning("Unable to cancel TWAP %s: %s", algo_id, exc)

    def _twap_expired(self) -> bool:
        return self.state.active_twap_started_at > 0 and time.time() - self.state.active_twap_started_at >= self.state.twap_duration_seconds

    def _twap_quote_budget(self) -> Decimal:
        available = self.state.budget_remaining if self.state.budget_remaining > 0 else self.state.budget_total
        if not self.dry_run:
            free_quote = self._quote_asset_free_balance()
            if free_quote > Decimal("0"):
                available = min(available, free_quote)
        if available <= 0:
            self.state.recommended_twap_quote_budget = Decimal("0")
            self.state.last_risk_note = "No available budget"
            return Decimal("0")

        equity = self._current_equity_usdt()
        risk_usdt = (equity * self.state.equity_risk_per_trade_pct).quantize(Decimal("0.01"))
        stop_move_pct = self.state.risk_stop_move_pct if self.state.risk_stop_move_pct > 0 else Decimal("0.012")
        if risk_usdt <= 0:
            self.state.recommended_twap_quote_budget = Decimal("0")
            self.state.last_risk_note = "Risk budget is zero"
            return Decimal("0")

        budget_by_risk = risk_usdt / stop_move_pct
        balance_cap = (available * self.state.adaptive_max_entry_balance_fraction).quantize(Decimal("0.01"))
        budget = min(available, self.state.max_twap_quote_budget_usdt, budget_by_risk, balance_cap)
        if budget <= 0:
            self.state.recommended_twap_quote_budget = Decimal("0")
            self.state.last_risk_note = "Risk-sized budget is zero"
            return Decimal("0")

        budget = budget.quantize(Decimal("0.01"))
        self.state.recommended_twap_quote_budget = budget
        # Apply minimum floor only when we have enough available balance to meet it.
        min_floor = min(available, self.state.min_twap_quote_budget_usdt)
        if available >= self.state.min_twap_quote_budget_usdt and budget < min_floor:
            budget = min_floor.quantize(Decimal("0.01"))
        self.state.last_risk_note = (
            f"Equity ${float(equity):.2f}, risk ${float(risk_usdt):.2f}, stop move {float(stop_move_pct * Decimal('100')):.2f}%, "
            f"risk-sized budget ${float(budget_by_risk):.2f}, balance cap ${float(balance_cap):.2f} "
            f"({float(self.state.adaptive_max_entry_balance_fraction * Decimal('100')):.0f}% of available), floor ${float(min_floor):.2f}, selected ${float(budget):.2f}."
        )
        return budget

    def _normalize_adaptive_strategy_parameters(self) -> None:
        self.state.adaptive_dca_levels = max(1, min(self.dca_levels, int(self.state.adaptive_dca_levels or self.dca_levels)))
        self.state.adaptive_dca_reprice_interval_seconds = max(60, min(600, int(self.state.adaptive_dca_reprice_interval_seconds or self.dca_reprice_interval_seconds)))
        self.state.adaptive_dca_min_step_pct = max(Decimal("0.001"), min(self.dca_max_step_pct, Decimal(str(self.state.adaptive_dca_min_step_pct or self.dca_min_step_pct))))
        self.state.adaptive_dca_max_step_pct = max(self.state.adaptive_dca_min_step_pct, min(self.dca_max_step_pct, Decimal(str(self.state.adaptive_dca_max_step_pct or self.dca_max_step_pct))))
        self.state.adaptive_max_entry_balance_fraction = max(Decimal("0.01"), min(self.max_entry_balance_fraction, Decimal(str(self.state.adaptive_max_entry_balance_fraction or self.max_entry_balance_fraction))))

    def _cycle_max_loss_usdt(self) -> Decimal:
        # Prefer position-size-based stop (scales with how much is invested)
        reference = self.state.position_cost_basis if self.state.position_cost_basis > 0 else self.state.active_twap_quote_budget
        if reference > 0:
            dynamic_stop = (reference * self.state.adaptive_stop_loss_pct).quantize(Decimal("0.01"))
        else:
            # Fallback: equity-fraction risk before any position is open
            dynamic_stop = (self._current_equity_usdt() * self.state.equity_risk_per_trade_pct).quantize(Decimal("0.01"))
        if dynamic_stop <= 0:
            return self.min_stop_loss_usdt.quantize(Decimal("0.01"))
        return min(self.max_order_loss_usdt, max(self.min_stop_loss_usdt, dynamic_stop)).quantize(Decimal("0.01"))

    def _active_stop_loss_usdt(self) -> Decimal:
        reference = self.state.position_cost_basis if self.state.position_cost_basis > 0 else self.state.active_twap_quote_budget
        stop_by_move = (reference * self.state.risk_stop_move_pct).quantize(Decimal("0.01")) if reference > 0 else Decimal("0")
        stop_by_move = min(self.max_order_loss_usdt, max(self.min_stop_loss_usdt, stop_by_move))
        if self.state.max_loss_usdt > 0:
            return min(self.state.max_loss_usdt, stop_by_move).quantize(Decimal("0.01"))
        return stop_by_move.quantize(Decimal("0.01"))

    def _reset_cycle_state(self, keep_cycle_count: bool = True) -> None:
        cycles = self.state.cycles_completed
        self.state.active_twap_algo_id = None
        self.state.active_twap_client_algo_id = None
        self.state.active_twap_status = "idle"
        self.state.active_twap_started_at = 0.0
        self.state.active_twap_quote_budget = Decimal("0")
        self.state.active_twap_executed_quote = Decimal("0")
        self.state.dca_last_repriced_at = 0.0
        self.state.dca_order_count = 0
        self.state.position_qty = Decimal("0")
        self.state.position_free_qty = Decimal("0")
        self.state.position_locked_qty = Decimal("0")
        self.state.position_cost_basis = Decimal("0")
        self.state.cost_basis_source = "none"
        self.state.entry_price = None
        self.state.current_profit_usdt = Decimal("0")
        self.state.peak_profit_usdt = Decimal("0")
        self.state.entry_time = 0.0
        self.state.budget_remaining = self.state.budget_total
        self._clear_target_sell_order()
        self._clear_stop_loss_order()
        if keep_cycle_count:
            self.state.cycles_completed = cycles

    def _round_quantity(self, quantity: Decimal) -> Decimal:
        step = self._get_quantity_step_size()
        steps = (quantity / step).to_integral_value(rounding=ROUND_DOWN)
        return steps * step

    def _get_bitget_account_mode(self, force: bool = False) -> tuple[str, str]:
        """Return Bitget API account mode without changing account state."""
        now = time.time()
        if not force and self._bitget_account_mode_cache and now - self._bitget_account_mode_cache[0] < 300:
            return self._bitget_account_mode_cache[1], self._bitget_account_mode_cache[2]
        if not all((self.bitget_api_key, self.bitget_api_secret, self.bitget_passphrase)):
            result = ("credentials_missing", "Bitget credentials are incomplete in .env")
        else:
            try:
                self._bitget_private_request("GET", "/api/v3/account/settings", {})
            except Exception as exc:
                message = str(exc)
                if "40084" in message or "classic account" in message.lower():
                    result = ("classic", "Bitget Classic account detected; UTA APIs are unavailable")
                else:
                    result = ("unknown", f"Unable to verify Bitget account mode: {message[:160]}")
            else:
                result = ("uta", "Bitget Unified Trading Account API is available")
        self._bitget_account_mode_cache = (now, result[0], result[1])
        return result

    def _stock_trading_block_reason(self, symbol: str | None = None) -> str | None:
        if self.primary_exchange != "bitget" or not self.is_stock_instrument(symbol):
            return None
        mode, detail = self._get_bitget_account_mode()
        if mode == "uta":
            return None
        if mode == "classic":
            return (
                "Automated Reality stock orders require a Bitget Unified Trading Account and a UID "
                "whitelisted for the Reality order API. This API key belongs to a Classic account."
            )
        return f"Automated Reality stock orders are unavailable. {detail}"

    def _bitget_api_symbol(self, symbol: str | None = None) -> str:
        normalized = self._normalize_symbol(symbol)
        metadata = self.get_instrument_metadata(normalized)
        return str(metadata.get("api_symbol") or metadata.get("symbol") or normalized)

    def _send_bitget_signed_request(self, method: str, path: str, params: dict) -> Any:
        if not all((self.bitget_api_key, self.bitget_api_secret, self.bitget_passphrase)):
            raise RuntimeError("Bitget credentials are incomplete in .env")
        method = method.upper()
        payload = dict(params or {})
        request_symbol = self._normalize_symbol(str(payload.get("symbol") or self.symbol))
        is_stock = self.is_stock_instrument(request_symbol)
        if path == "/api/v3/account":
            mode, _ = self._get_bitget_account_mode()
            if mode == "uta":
                raw = self._bitget_private_request("GET", "/api/v3/account/assets", {})
                assets = (raw.get("data") or {}).get("assets") or []
                return {"balances": [{"asset": str(item.get("coin") or "").upper(), "free": item.get("available", "0"), "locked": item.get("locked", "0")} for item in assets]}
            raw = self._bitget_private_request("GET", "/api/v2/spot/account/assets", {})
            return {"balances": [{"asset": str(item.get("coin") or "").upper(), "free": item.get("available", "0"), "locked": str(Decimal(str(item.get("frozen", "0"))) + Decimal(str(item.get("locked", "0"))))} for item in raw.get("data", [])]}
        if path == "/api/v3/openOrders":
            mode, _ = self._get_bitget_account_mode()
            if mode == "uta":
                query = {"category": "SPOT", "symbol": self._bitget_api_symbol(request_symbol)} if payload.get("symbol") else {"category": "SPOT"}
                raw = self._bitget_private_request("GET", "/api/v3/trade/unfilled-orders", query)
                return [self._bitget_order_to_binance(item) for item in (raw.get("data") or {}).get("list", [])]
            raw = self._bitget_private_request("GET", "/api/v2/spot/trade/unfilled-orders", {"symbol": payload.get("symbol")} if payload.get("symbol") else {})
            return [self._bitget_order_to_binance(item) for item in raw.get("data", [])]
        if path == "/api/v3/myTrades":
            mode, _ = self._get_bitget_account_mode()
            if mode == "uta":
                raw = self._bitget_private_request("GET", "/api/v3/trade/fills", {"category": "SPOT", "limit": min(100, int(payload.get("limit", 100)))})
                fills = (raw.get("data") or {}).get("list", [])
                if payload.get("symbol"):
                    fills = [item for item in fills if str(item.get("symbol") or "").upper() == request_symbol]
                return [self._bitget_fill_to_binance(item) for item in fills]
            raw = self._bitget_private_request("GET", "/api/v2/spot/trade/fills", {"symbol": payload.get("symbol"), "limit": payload.get("limit", 1000)} if payload.get("symbol") else {"limit": payload.get("limit", 1000)})
            return [self._bitget_fill_to_binance(item) for item in raw.get("data", [])]
        if path == "/sapi/v1/asset/tradeFee":
            return []
        if path == "/api/v3/order" and method == "GET":
            mode, _ = self._get_bitget_account_mode()
            if mode == "uta":
                query = {"orderId": str(payload.get("orderId"))} if payload.get("orderId") else {"clientOid": str(payload.get("origClientOrderId") or payload.get("clientOrderId") or "")}
                raw = self._bitget_private_request("GET", "/api/v3/trade/order-info", query)
                return self._bitget_order_to_binance(raw.get("data") or {})
            query = {"symbol": payload.get("symbol"), "orderId": str(payload.get("orderId"))}
            raw = self._bitget_private_request("GET", "/api/v2/spot/trade/orderInfo", query)
            data = raw.get("data") or []
            item = data[0] if isinstance(data, list) and data else data
            return self._bitget_order_to_binance(item or {})
        if path == "/api/v3/order" and method == "DELETE":
            if is_stock:
                block_reason = self._stock_trading_block_reason(request_symbol)
                if block_reason:
                    raise RuntimeError(block_reason)
                cancel_payload = {"symbol": self._bitget_api_symbol(request_symbol)}
                if payload.get("orderId"):
                    cancel_payload["orderId"] = str(payload["orderId"])
                elif payload.get("origClientOrderId") or payload.get("clientOrderId"):
                    cancel_payload["clientOid"] = str(payload.get("origClientOrderId") or payload.get("clientOrderId"))
                raw = self._bitget_private_request("POST", "/api/v3/trade/cancel-reality-order", cancel_payload)
                return raw.get("data") or raw
            raw = self._bitget_private_request("POST", "/api/v2/spot/trade/cancel-order", {"symbol": payload.get("symbol"), "orderId": str(payload.get("orderId"))})
            return raw.get("data") or raw
        if path == "/api/v3/order" and method == "POST":
            side = str(payload.get("side") or "").upper()
            order_type = str(payload.get("type") or "").upper()
            if is_stock:
                block_reason = self._stock_trading_block_reason(request_symbol)
                if block_reason:
                    raise RuntimeError(block_reason)
                if order_type not in {"MARKET", "LIMIT"}:
                    raise RuntimeError("Bitget Reality stock API supports MARKET and LIMIT orders only; stock stop-losses are software monitored")
                quantity_value = payload.get("quoteOrderQty") if side == "BUY" and order_type == "MARKET" else payload.get("quantity")
                order = {
                    "symbol": self._bitget_api_symbol(request_symbol),
                    "side": side.lower(),
                    "orderType": order_type.lower(),
                    "qty": format(Decimal(str(quantity_value or "0")), "f"),
                }
                if order_type == "LIMIT":
                    order["price"] = format(Decimal(str(payload.get("price", "0"))), "f")
                if payload.get("newClientOrderId"):
                    order["clientOid"] = str(payload["newClientOrderId"])
                raw = self._bitget_private_request("POST", "/api/v3/trade/place-reality-order", order)
                data = raw.get("data") or {}
                return {"orderId": data.get("orderId"), "clientOrderId": data.get("clientOid"), "status": "NEW", "symbol": request_symbol, "side": side}
            order: dict[str, Any] = {"symbol": payload["symbol"], "side": side.lower(), "orderType": "market" if order_type == "MARKET" else "limit"}
            if order_type == "MARKET":
                if side == "BUY":
                    order["size"] = format(Decimal(str(payload.get("quoteOrderQty", "0"))), "f")
                else:
                    order["size"] = format(Decimal(str(payload.get("quantity", "0"))), "f")
            else:
                order.update({"force": str(payload.get("timeInForce") or "gtc").lower(), "size": format(Decimal(str(payload.get("quantity", "0"))), "f"), "price": format(Decimal(str(payload.get("price", "0"))), "f")})
            if payload.get("newClientOrderId"):
                order["clientOid"] = str(payload["newClientOrderId"])
            raw = self._bitget_private_request("POST", "/api/v2/spot/trade/place-order", order)
            data = raw.get("data") or {}
            return {"orderId": data.get("orderId"), "clientOrderId": data.get("clientOid"), "status": "NEW", "symbol": payload.get("symbol"), "side": side}
        raise RuntimeError(f"Bitget primary exchange does not support Binance endpoint {method} {path}")

    def _bitget_private_request(self, method: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        method = method.upper()
        query = urllib.parse.urlencode({key: value for key, value in params.items() if value not in (None, "")}) if method == "GET" else ""
        request_path = f"{path}?{query}" if query else path
        body = "" if method == "GET" else json.dumps({key: value for key, value in params.items() if value is not None}, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        prehash = timestamp + method + request_path + body
        signature = base64.b64encode(hmac.new(self.bitget_api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()).decode("utf-8")
        request = urllib.request.Request(
            f"{BITGET_BASE_URL}{request_path}",
            data=body.encode("utf-8") if body else None,
            headers={"ACCESS-KEY": self.bitget_api_key, "ACCESS-SIGN": signature, "ACCESS-TIMESTAMP": timestamp, "ACCESS-PASSPHRASE": self.bitget_passphrase, "Content-Type": "application/json", "locale": "en-US", "User-Agent": "twap-swing-bot"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Bitget API {path} failed: HTTP {exc.code} {body_text}") from exc
        if str(result.get("code", "00000")) != "00000":
            raise RuntimeError(f"Bitget API {path} failed: {result.get('code')} {result.get('msg')}")
        return result

    @staticmethod
    def _bitget_order_to_binance(item: dict[str, Any]) -> dict[str, Any]:
        status = str(item.get("orderStatus") or item.get("status") or "live").lower()
        return {
            "orderId": item.get("orderId"),
            "clientOrderId": item.get("clientOid"),
            "symbol": str(item.get("symbol") or "").upper(),
            "side": str(item.get("side") or "").upper(),
            "type": str(item.get("orderType") or "").upper(),
            "timeInForce": str(item.get("timeInForce") or item.get("force") or "").upper(),
            "status": {
                "live": "NEW",
                "new": "NEW",
                "partially_filled": "PARTIALLY_FILLED",
                "filled": "FILLED",
                "cancelled": "CANCELED",
                "canceled": "CANCELED",
            }.get(status, status.upper()),
            "price": item.get("price") or item.get("priceAvg") or item.get("avgPrice") or "0",
            "origQty": item.get("qty", item.get("size", "0")),
            "executedQty": item.get("cumExecQty", item.get("baseVolume", "0")),
            "cummulativeQuoteQty": item.get("cumExecValue", item.get("quoteVolume", "0")),
            "time": item.get("createdTime", item.get("cTime", 0)),
        }

    @staticmethod
    def _bitget_fill_to_binance(item: dict[str, Any]) -> dict[str, Any]:
        side = str(item.get("side") or "").lower()
        commission = Decimal("0")
        commission_asset = ""
        fee_detail = item.get("feeDetail") or {}
        if isinstance(fee_detail, str):
            try:
                fee_detail = json.loads(fee_detail)
            except json.JSONDecodeError:
                fee_detail = {}
        if isinstance(fee_detail, list):
            for detail in fee_detail:
                if not isinstance(detail, dict):
                    continue
                fee_value = Decimal(str(detail.get("fee") or "0"))
                if fee_value:
                    commission = abs(fee_value)
                    commission_asset = str(detail.get("feeCoin") or "").upper()
                    break
        elif isinstance(fee_detail, dict):
            for fee_coin, detail in fee_detail.items():
                if fee_coin == "newFees" or not isinstance(detail, dict):
                    continue
                fee_value = Decimal(str(detail.get("totalFee") or "0"))
                if fee_value:
                    commission = abs(fee_value)
                    commission_asset = str(detail.get("feeCoinCode") or fee_coin).upper()
                    break
        return {
            "id": item.get("execId", item.get("tradeId")),
            "orderId": item.get("orderId"),
            "symbol": str(item.get("symbol") or "").upper(),
            "time": int(item.get("createdTime") or item.get("fillTime") or item.get("cTime") or 0),
            "price": item.get("execPrice", item.get("priceAvg", item.get("price", "0"))),
            "qty": item.get("execQty", item.get("size", item.get("baseVolume", "0"))),
            "quoteQty": item.get("execValue", item.get("amount", item.get("quoteVolume", "0"))),
            "commission": format(commission, "f"),
            "commissionAsset": commission_asset,
            "isBuyer": side == "buy",
            "isMaker": str(item.get("tradeScope") or "").lower() == "maker",
        }

    def _send_signed_request(self, method: str, path: str, params: dict) -> dict:
        if self.primary_exchange == "bitget":
            return self._send_bitget_signed_request(method, path, params)
        query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")
        ts = int(time.time() * 1000)
        query += f"&timestamp={ts}&recvWindow=5000"
        sig = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{BINANCE_BASE_URL}{path}?{query}&signature={sig}"
        req = urllib.request.Request(
            url,
            data=b"" if method in {"POST", "DELETE"} else None,
            headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "twap-swing-bot"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = body
            raise RuntimeError(f"Binance API {path} failed: HTTP {exc.code} {payload}") from exc


VolatilityTwapBot = TwapSwingBot
