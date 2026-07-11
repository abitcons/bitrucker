from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .volatility_bot import VolatilityTwapBot


class TenantBotFleet:
    """Tenant-scoped paper/live bot collection.

    The fleet owns its credentials, runtime directory, pair registry, state files,
    and entry guard. Callers cannot address a bot outside this namespace.
    """

    def __init__(
        self,
        root_dir: Path,
        user_id: int,
        *,
        primary_exchange: str = "binance",
        exchange_credentials: dict[str, dict[str, str]] | None = None,
        dry_run: bool = True,
        max_pairs: int = 4,
    ) -> None:
        self.user_id = int(user_id)
        self.root_dir = root_dir / "runtime" / "tenants" / f"user_{self.user_id}"
        self.root_dir.mkdir(parents=True, exist_ok=True)
        normalized_exchange = primary_exchange.strip().lower()
        self.primary_exchange = normalized_exchange if normalized_exchange in {"binance", "bitget"} else "binance"
        self.exchange_credentials = exchange_credentials or {}
        self.dry_run = bool(dry_run)
        self.max_pairs = max(1, min(20, int(max_pairs)))
        self.lock = threading.RLock()
        self.bots: dict[str, VolatilityTwapBot] = {}
        self.default_symbol = ""
        self._catalog_bot: VolatilityTwapBot | None = None
        self.managed_pairs_file = self.root_dir / "managed_pairs.json"
        self._restore()

    def _bot_root(self, symbol: str) -> Path:
        return self.root_dir / f"bot_{symbol.lower()}"

    @staticmethod
    def _normalize_symbol(symbol: str | None) -> str:
        return (symbol or "").upper().replace("/", "").strip()

    def _new_bot(self, root: Path, primary_exchange: str | None = None) -> VolatilityTwapBot:
        binance = self.exchange_credentials.get("binance") or {}
        bot = VolatilityTwapBot(
            root,
            str(binance.get("api_key") or ""),
            str(binance.get("api_secret") or ""),
            dry_run=self.dry_run,
            primary_exchange=primary_exchange or self.primary_exchange,
            exchange_credentials=self.exchange_credentials,
        )
        # Tenant paper workers must never inherit the operator's notification or
        # decision-daemon integrations from process-level environment variables.
        bot.notifications_enabled = False
        bot.codex_decision_enabled = False
        bot.entry_guard = self._entry_guard
        return bot

    def _persist(self) -> None:
        with self.lock:
            payload = {
                "tenant_user_id": self.user_id,
                "default_symbol": self.default_symbol,
                "pairs": sorted(self.bots),
                "primary_exchange": self.primary_exchange,
                "dry_run": self.dry_run,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        temporary = self.managed_pairs_file.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.managed_pairs_file)

    def _restore(self) -> None:
        try:
            payload = json.loads(self.managed_pairs_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return
        if int(payload.get("tenant_user_id") or self.user_id) != self.user_id:
            raise RuntimeError("Tenant fleet state owner mismatch")
        configured_exchange = str(payload.get("primary_exchange") or self.primary_exchange).lower()
        if configured_exchange in {"binance", "bitget"}:
            self.primary_exchange = configured_exchange
        requested_pairs = [self._normalize_symbol(item) for item in payload.get("pairs") or []]
        for symbol in requested_pairs[: self.max_pairs]:
            if not symbol:
                continue
            try:
                self.add_pair(symbol, auto_start=False, persist=False)
            except Exception:
                continue
        requested_default = self._normalize_symbol(payload.get("default_symbol"))
        if requested_default in self.bots:
            self.default_symbol = requested_default

    def all_pairs(self) -> list[str]:
        with self.lock:
            return sorted(self.bots)

    def bots_snapshot(self) -> dict[str, VolatilityTwapBot]:
        with self.lock:
            return dict(self.bots)

    def add_pair(
        self,
        symbol: str,
        *,
        auto_start: bool = False,
        persist: bool = True,
        primary_exchange: str | None = None,
    ) -> VolatilityTwapBot:
        requested = self._normalize_symbol(symbol)
        if not requested:
            raise ValueError("Symbol is required")
        selected_exchange = str(primary_exchange or self.primary_exchange).strip().lower()
        if selected_exchange not in {"binance", "bitget"}:
            raise ValueError("primary exchange must be BINANCE or BITGET")
        with self.lock:
            existing = self.bots.get(requested)
            if existing:
                return existing
            if len(self.bots) >= self.max_pairs:
                raise ValueError(f"Plan limit reached: maximum {self.max_pairs} managed pairs")

        provisional_root = self._bot_root(requested)
        provisional_root.mkdir(parents=True, exist_ok=True)
        bot = self._new_bot(provisional_root, selected_exchange)
        bot.switch_symbol(requested)
        resolved = bot.symbol

        with self.lock:
            existing = self.bots.get(resolved)
            if existing:
                return existing
            self.bots[resolved] = bot
            if not self.default_symbol:
                self.default_symbol = resolved
        if persist:
            self._persist()
        if auto_start:
            bot.start()
        return bot

    def set_primary_exchange(self, exchange: str, symbol: str | None = None) -> str:
        """Select the exchange for discovery/new pairs or switch one idle pair."""
        normalized_exchange = str(exchange or "").strip().lower()
        if normalized_exchange not in {"binance", "bitget"}:
            raise ValueError("primary exchange must be BINANCE or BITGET")
        normalized_symbol = self._normalize_symbol(symbol)
        with self.lock:
            bot = self.bots.get(normalized_symbol) if normalized_symbol else None
        if normalized_symbol and bot is None:
            raise ValueError(f"Pair {normalized_symbol} is not active. Add it first.")
        if bot is not None:
            message = bot.set_primary_exchange(normalized_exchange)
        else:
            message = f"Primary exchange for new paper pairs set to {normalized_exchange.upper()}"
        with self.lock:
            self.primary_exchange = normalized_exchange
            self._catalog_bot = None
        self._persist()
        return message

    def supported_instruments(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return the selected exchange's public, tenant-safe instrument list."""
        with self.lock:
            bot = next(
                (item for item in self.bots.values() if item.primary_exchange == self.primary_exchange),
                None,
            )
            if bot is None:
                if self._catalog_bot is None:
                    self._catalog_bot = self._new_bot(self.root_dir / "catalog", self.primary_exchange)
                bot = self._catalog_bot
        symbols = bot.get_discovery_symbols(limit=limit)
        return [
            {
                "symbol": symbol,
                "instrument_type": bot.get_instrument_type(symbol),
                "is_stock": bot.is_stock_instrument(symbol),
            }
            for symbol in symbols
        ]

    def remove_pair(self, symbol: str) -> tuple[str, str | None]:
        normalized = self._normalize_symbol(symbol)
        with self.lock:
            bot = self.bots.get(normalized)
            if bot is None:
                raise ValueError(f"Pair {normalized} is not active")
            if bot.state.position_qty > 0 or bot.state.target_sell_order_id or bot.state.stop_loss_order_id:
                raise RuntimeError("Close the paper/live position and orders before removing this pair")
            self.bots.pop(normalized)
            if self.default_symbol == normalized:
                self.default_symbol = sorted(self.bots)[0] if self.bots else ""
        if bot.running:
            bot.stop()
        self._persist()
        return normalized, self.default_symbol or None

    def get_bot(self, symbol: str | None = None, *, create_if_missing: bool = False) -> VolatilityTwapBot:
        normalized = self._normalize_symbol(symbol)
        with self.lock:
            if normalized:
                bot = self.bots.get(normalized)
            else:
                bot = self.bots.get(self.default_symbol) if self.default_symbol else None
        if bot:
            return bot
        if create_if_missing and normalized:
            return self.add_pair(normalized, auto_start=False)
        if normalized:
            raise ValueError(f"Pair {normalized} is not active. Add it first.")
        raise ValueError("No active pair. Add a pair first.")

    def _entry_guard(self, symbol: str) -> tuple[bool, str]:
        normalized = self._normalize_symbol(symbol)
        with self.lock:
            other_exposure = [
                pair
                for pair, bot in self.bots.items()
                if pair != normalized
                and (
                    bot.state.position_cost_basis >= bot.dust_position_cost_usdt
                    or bool(bot.state.active_twap_algo_id)
                    or bool(bot.state.target_sell_order_id)
                )
            ]
            symbols = sorted(self.bots)
            rank_bot = self.bots.get(self.default_symbol) if self.default_symbol else None
        if other_exposure:
            return False, "Entry blocked: tenant exposure already open on " + ", ".join(sorted(other_exposure))
        if len(symbols) <= 1 or rank_bot is None:
            return True, "Only one tenant pair"
        payload = rank_bot.get_pair_candidates(force=False, symbols=symbols, max_seconds=5.0)
        best = payload.get("best") if isinstance(payload, dict) else None
        if not isinstance(best, dict) or not best.get("symbol"):
            return False, "Entry blocked: candidate rank unavailable"
        best_symbol = self._normalize_symbol(str(best["symbol"]))
        return (
            (True, f"Entry allowed: {normalized} is tenant best candidate")
            if normalized == best_symbol
            else (False, f"Entry blocked: tenant best candidate is {best_symbol}")
        )

    def close(self) -> None:
        with self.lock:
            bots = list(self.bots.values())
        for bot in bots:
            if bot.running:
                bot.stop()
