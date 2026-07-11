from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Callable

from .binance_client import BinanceClient
from .bitget_client import BitgetClient
from .config import AssetRule, Settings
from .portfolio import AssetSnapshot, PortfolioService, render_snapshots
from .state import StateStore
from .telegram_client import TelegramNotifier


LOGGER = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


CONFIRMATION_GRACE_SECONDS = 120
CANCEL_CONFIRMATION_TIMEOUT_SECONDS = 20
CANCEL_CONFIRMATION_POLL_SECONDS = 2
PROFIT_HISTORY_LIMIT = 96


def _fmt_decimal(value: Decimal | None, decimals: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f}"


def _value_class(value: Decimal | None) -> str:
    if value is None:
        return "neutral"
    if value > 0:
        return "positive"
    if value < 0:
        return "negative"
    return "neutral"


def _algo_status_class(status: str | None) -> str:
    normalized = str(status or "UNKNOWN").upper()
    if normalized == "WORKING":
        return "pill-gold"
    if normalized == "PAUSED":
        return "pill-red"
    if normalized == "IDLE":
        return "pill-slate"
    if normalized in {"SUBMITTED", "FINISHED"}:
        return "pill-teal"
    if normalized in {"MISSING", "CANCELLED", "EXPIRED", "REJECTED", "FAILED"}:
        return "pill-red"
    return "pill-slate"


def _operation_status_class(allowed: bool) -> str:
    return "pill-teal" if allowed else "pill-red"


def _portfolio_target_status_class(status: str) -> str:
    normalized = str(status).upper()
    if normalized == "ACTIVE":
        return "pill-teal"
    if normalized == "TRIGGERED":
        return "pill-red"
    return "pill-slate"


class TradingStrategy:
    def __init__(
        self,
        settings: Settings,
        client: BinanceClient,
        portfolio: PortfolioService,
        notifier: TelegramNotifier,
        state: StateStore,
        bitget: BitgetClient | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.portfolio = portfolio
        self.notifier = notifier
        self.state = state
        self.bitget = bitget
        self.entry_guard: Callable[[str], tuple[bool, str]] | None = None

    def print_summary(self) -> list[AssetSnapshot]:
        snapshots = self.portfolio.build_snapshots()
        print(render_snapshots(snapshots))
        return snapshots

    # ------------------------------------------------------------------
    # Bitget mirror
    # ------------------------------------------------------------------
    def _mirror_to_bitget(self, symbol: str, side: str, quantity: Decimal, reason: str) -> None:
        if self.bitget is None:
            return
        try:
            if side.upper() == "BUY":
                response = self.bitget.market_buy(symbol, quantity)
            else:
                response = self.bitget.market_sell(symbol, quantity)

            order_record = {
                "order_id": str(response.get("data", {}).get("orderId") if isinstance(response.get("data"), dict) else response.get("orderId", "")),
                "symbol": symbol,
                "side": side.upper(),
                "quantity": str(quantity),
                "reason": reason,
                "status": "FILLED" if not self.settings.dry_run else "DRY_RUN",
                "created_at": iso_now(),
                "response": response,
            }
            self.state.append_bitget_order(order_record)
            LOGGER.info("Bitget mirror %s %s qty=%s reason=%s", side.upper(), symbol, quantity, reason)
        except Exception as exc:
            error_record = {
                "order_id": "",
                "symbol": symbol,
                "side": side.upper(),
                "quantity": str(quantity),
                "reason": reason,
                "status": "FAILED",
                "error": str(exc),
                "created_at": iso_now(),
                "response": None,
            }
            self.state.append_bitget_order(error_record)
            LOGGER.warning("Bitget mirror failed for %s %s qty=%s: %s", side.upper(), symbol, quantity, exc)
            self.notifier.send(f"Bitget mirror failed for {side.upper()} {symbol}: {exc}")

    def _get_asset_controls(self) -> dict[str, dict]:
        return self.state.get_asset_controls()

    def _is_asset_allowed(self, asset: str) -> bool:
        control = self._get_asset_controls().get(asset.upper())
        if control is None:
            return True
        return bool(control.get("allowed", True))

    def _set_asset_allowed(self, asset: str, allowed: bool, reason: str) -> None:
        self.state.set_asset_control(
            asset,
            {
                "allowed": allowed,
                "reason": reason,
                "updated_at": iso_now(),
            },
        )

    @staticmethod
    def _remote_order_started_at(remote_order: dict) -> str:
        book_time = remote_order.get("bookTime")
        if book_time is None:
            return iso_now()
        try:
            return datetime.fromtimestamp(int(book_time) / 1000, UTC).isoformat()
        except (TypeError, ValueError, OSError):
            return iso_now()

    def _adopt_remote_managed_cycles(
        self,
        open_orders_index: dict[str, dict[str, dict] | dict[str, list[dict]]],
        existing_cycles: dict[str, dict],
    ) -> dict[str, dict]:
        adopted: dict[str, dict] = {}
        for asset, rule in self.settings.assets.items():
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)
            if not symbol:
                continue

            symbol_orders = open_orders_index["by_symbol"].get(symbol, [])
            working_order = next((order for order in symbol_orders if order.get("algoStatus") == "WORKING"), None)
            if working_order is None:
                continue

            existing_cycle = existing_cycles.get(symbol)
            if existing_cycle is not None and str(existing_cycle.get("algo_status", "")).upper() == "WORKING":
                adopted[symbol] = existing_cycle
                continue

            base_cycle = {
                "asset": asset,
                "symbol": symbol,
                "side": str(working_order.get("side") or "BUY"),
                "started_at": self._remote_order_started_at(working_order),
                "confirmation_deadline": None,
                "algo_open": True,
                "submitted_qty": "0",
                "executed_qty": "0",
                "executed_amt": "0",
                "avg_price": "0",
                "total_budget_usd": str(rule.reentry_quote_budget_usd or self.settings.default_reentry_quote_budget_usd),
                "duration_seconds": self._resolve_twap_duration_seconds(rule),
                "profit_target_usd": str(self._profit_target_for_asset(asset, None)),
                "notified_sub_orders": list(existing_cycle.get("notified_sub_orders", [])) if existing_cycle else [],
            }
            adopted_cycle = self._merge_remote_order(base_cycle, working_order)
            self.state.upsert_cycle(symbol, adopted_cycle)
            adopted[symbol] = adopted_cycle

        return adopted

    def build_dashboard_view(self) -> dict[str, object]:
        snapshots = self.portfolio.build_snapshots()
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots if snapshot.symbol}
        snapshot_by_asset = {snapshot.asset: snapshot for snapshot in snapshots}
        asset_controls = self._get_asset_controls()
        profit_history_by_asset = self.state.get_profit_history()
        self._seed_managed_profit_history(snapshots, profit_history_by_asset)
        warnings: list[str] = []
        now = utc_now()
        open_orders_index: dict[str, dict[str, dict] | dict[str, list[dict]]] = {"by_algo_id": {}, "by_client_algo_id": {}, "by_symbol": {}}

        if not self.settings.dry_run:
            try:
                open_orders_index = self._get_open_algo_orders_index()
            except RuntimeError as exc:
                warnings.append(str(exc))

        cycles_state = self.state.get_cycles()
        if not self.settings.dry_run and not warnings:
            cycles_state.update(self._adopt_remote_managed_cycles(open_orders_index, cycles_state))
        current_cycles_by_symbol: dict[str, dict] = {}
        cycle_cards: list[dict[str, object]] = []
        for symbol, cycle in list(cycles_state.items()):
            current_cycle = dict(cycle)
            if not self.settings.dry_run and not warnings:
                current_cycle = self._refresh_cycle(symbol, current_cycle, open_orders_index)
            current_cycles_by_symbol[symbol] = current_cycle

            snapshot = snapshot_by_symbol.get(symbol)
            submitted_qty = Decimal(str(current_cycle.get("submitted_qty", "0")))
            executed_qty = Decimal(str(current_cycle.get("executed_qty", "0")))
            progress_pct = Decimal("0")
            if submitted_qty > 0:
                progress_pct = min(Decimal("100"), executed_qty / submitted_qty * Decimal("100"))
            algo_status = str(current_cycle.get("algo_status", "UNKNOWN"))
            target_profit = self._profit_target_for_asset(str(current_cycle.get("asset", "")).upper(), current_cycle)

            cycle_cards.append(
                {
                    "asset": current_cycle.get("asset", "-"),
                    "symbol": symbol,
                    "algo_status": algo_status,
                    "algo_status_class": _algo_status_class(algo_status),
                    "client_algo_id": current_cycle.get("client_algo_id") or "-",
                    "algo_id": current_cycle.get("algo_id") or "-",
                    "submitted_qty_display": _fmt_decimal(submitted_qty, 8),
                    "executed_qty_display": _fmt_decimal(executed_qty, 8),
                    "executed_amt_display": _fmt_decimal(Decimal(str(current_cycle.get("executed_amt", "0"))), 2),
                    "avg_price_display": _fmt_decimal(Decimal(str(current_cycle.get("avg_price", "0"))), 4),
                    "budget_display": _fmt_decimal(Decimal(str(current_cycle.get("total_budget_usd", "0"))), 2),
                    "profit_target_display": _fmt_decimal(target_profit, 2),
                    "duration_seconds": int(current_cycle.get("duration_seconds", 0)),
                    "started_at": current_cycle.get("started_at", "-"),
                    "confirmation_deadline": current_cycle.get("confirmation_deadline") or "-",
                    "retry_in_seconds": self._seconds_until_retry(current_cycle, now),
                    "submit_response_display": self._format_submit_response(current_cycle.get("last_submit_response")),
                    "status_note": self._cycle_status_note(current_cycle, now),
                    "progress_pct": float(progress_pct),
                    "position_pnl_display": _fmt_decimal(snapshot.profit_usd if snapshot else None, 2),
                    "position_value_display": _fmt_decimal(snapshot.value_usd if snapshot else None, 2),
                    "position_pnl_class": _value_class(snapshot.profit_usd if snapshot else None),
                }
            )

        cycle_symbols = set(current_cycles_by_symbol.keys())
        active_cycle_count = len(cycle_cards)
        for asset, rule in self.settings.assets.items():
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)
            if not symbol or symbol in current_cycles_by_symbol:
                continue

            snapshot = snapshot_by_symbol.get(symbol) or snapshot_by_asset.get(asset)
            allowed = self._is_asset_allowed(asset)
            control = asset_controls.get(asset, {})
            if not allowed:
                algo_status = "PAUSED"
                status_note = str(control.get("reason") or "Strategy is paused until you allow it again.")
            elif not rule.start_cycle_when_flat:
                algo_status = "IDLE"
                status_note = "No active TWAP. Auto-start is disabled for this asset in strategy.json."
            else:
                algo_status = "IDLE"
                status_note = "No active TWAP. The bot will start this asset when it becomes eligible."

            cycle_cards.append(
                {
                    "asset": asset,
                    "symbol": symbol,
                    "algo_status": algo_status,
                    "algo_status_class": _algo_status_class(algo_status),
                    "client_algo_id": "-",
                    "algo_id": "-",
                    "submitted_qty_display": "-",
                    "executed_qty_display": "-",
                    "executed_amt_display": "-",
                    "avg_price_display": "-",
                    "budget_display": _fmt_decimal(rule.reentry_quote_budget_usd or self.settings.default_reentry_quote_budget_usd, 2),
                    "profit_target_display": _fmt_decimal(self._profit_target_for_asset(asset, None), 2),
                    "duration_seconds": self._resolve_twap_duration_seconds(rule),
                    "started_at": "-",
                    "confirmation_deadline": "-",
                    "retry_in_seconds": None,
                    "submit_response_display": "-",
                    "status_note": status_note,
                    "progress_pct": 0.0,
                    "position_pnl_display": _fmt_decimal(snapshot.profit_usd if snapshot else None, 2),
                    "position_value_display": _fmt_decimal(snapshot.value_usd if snapshot else None, 2),
                    "position_pnl_class": _value_class(snapshot.profit_usd if snapshot else None),
                }
            )

        operations: list[dict[str, object]] = []
        for asset, rule in self.settings.assets.items():
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets) or "-"
            snapshot = snapshot_by_asset.get(asset)
            cycle = current_cycles_by_symbol.get(symbol) if symbol != "-" else None
            allowed = self._is_asset_allowed(asset)
            control = asset_controls.get(asset, {})
            cycle_status = str(cycle.get("algo_status", "NONE")).upper() if cycle else "NONE"
            current_profit = snapshot.profit_usd if snapshot and snapshot.profit_usd is not None else Decimal("0")
            target_profit = self._profit_target_for_asset(asset, cycle)
            operation_budget_ring = self._build_operation_budget_ring(cycle, rule, allowed)
            operations.append(
                {
                    "asset": asset,
                    "symbol": symbol,
                    "operation_allowed": allowed,
                    "operation_status_label": "ALLOWED" if allowed else "PAUSED",
                    "operation_status_class": _operation_status_class(allowed),
                    "operation_note": (
                        "Strategy can start or restart this asset."
                        if allowed
                        else str(control.get("reason") or "Strategy is paused until you allow it again.")
                    ),
                    "holding_qty_display": _fmt_decimal(snapshot.free_quantity if snapshot else Decimal("0"), 8),
                    "holding_value_display": _fmt_decimal(snapshot.value_usd if snapshot else Decimal("0"), 2),
                    "cycle_status": cycle_status,
                    "budget_display": _fmt_decimal(rule.reentry_quote_budget_usd or self.settings.default_reentry_quote_budget_usd, 2),
                    "current_profit_display": _fmt_decimal(current_profit, 2),
                    "current_profit_class": _value_class(snapshot.profit_usd if snapshot else None),
                    "profit_target_display": _fmt_decimal(target_profit, 2),
                    "profit_chart": self._build_profit_chart(
                        profit_history_by_asset.get(asset, []),
                        current_profit=current_profit,
                        target_profit=target_profit,
                    ),
                    "budget_ring": operation_budget_ring,
                    "can_start": cycle_status not in {"WORKING", "SUBMITTED"},
                    "can_stop": allowed,
                    "can_allow": not allowed,
                }
            )

        snapshot_rows: list[dict[str, object]] = []
        for snapshot in snapshots:
            current_cycle = current_cycles_by_symbol.get(snapshot.symbol) if snapshot.symbol else None
            cycle_status = str(current_cycle.get("algo_status", "UNKNOWN")) if current_cycle else None
            asset_allowed = self._is_asset_allowed(snapshot.asset)
            control = asset_controls.get(snapshot.asset, {})
            snapshot_rows.append(
                {
                    "asset": snapshot.asset,
                    "symbol": snapshot.symbol or "-",
                    "quantity_display": _fmt_decimal(snapshot.quantity, 8),
                    "free_quantity_display": _fmt_decimal(snapshot.free_quantity, 8),
                    "value_display": _fmt_decimal(snapshot.value_usd, 2),
                    "avg_buy_display": _fmt_decimal(snapshot.avg_cost_usd, 4),
                    "cost_basis_display": _fmt_decimal(snapshot.cost_basis_usd, 2),
                    "profit_display": _fmt_decimal(snapshot.profit_usd, 2),
                    "profit_pct_display": _fmt_decimal(snapshot.profit_pct, 2),
                    "profit_class": _value_class(snapshot.profit_usd),
                    "note": snapshot.note or "",
                    "managed": snapshot.asset in self.settings.assets,
                    "has_cycle": snapshot.symbol in cycle_symbols if snapshot.symbol else False,
                    "cycle_status": cycle_status,
                    "cycle_status_class": _algo_status_class(cycle_status) if cycle_status else "",
                    "cycle_note": self._cycle_status_note(current_cycle, now) if current_cycle else "",
                    "operation_status_label": "ALLOWED" if asset_allowed and snapshot.asset in self.settings.assets else ("PAUSED" if snapshot.asset in self.settings.assets else ""),
                    "operation_status_class": _operation_status_class(asset_allowed) if snapshot.asset in self.settings.assets else "",
                    "operation_note": str(control.get("reason") or "") if snapshot.asset in self.settings.assets and not asset_allowed else "",
                    "can_sell": bool(snapshot.symbol and snapshot.free_quantity > 0 and snapshot.asset not in self.settings.exclude_assets),
                }
            )

        total_value = sum((snapshot.value_usd or Decimal("0") for snapshot in snapshots), Decimal("0"))
        total_profit = sum((snapshot.profit_usd or Decimal("0") for snapshot in snapshots if snapshot.profit_usd is not None), Decimal("0"))
        managed_profit = sum(
            (snapshot.profit_usd or Decimal("0"))
            for snapshot in snapshots
            if snapshot.asset in self.settings.assets and snapshot.profit_usd is not None
        )
        whole_strategy_target_view = self._build_whole_strategy_profit_target_view()
        portfolio_target_view = self._build_portfolio_value_target_view(total_value)

        return {
            "generated_at": iso_now(),
            "dry_run": self.settings.dry_run,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "profit_target_display": whole_strategy_target_view["target_display"],
            "profit_target_input_value": whole_strategy_target_view["input_value"],
            "profit_target_default_display": whole_strategy_target_view["default_display"],
            "profit_target_source_label": whole_strategy_target_view["source_label"],
            "profit_target_source_class": whole_strategy_target_view["source_class"],
            "profit_target_note": whole_strategy_target_view["note"],
            "profit_target_updated_at": whole_strategy_target_view["updated_at"],
            "can_clear_profit_target": whole_strategy_target_view["can_clear"],
            "total_value_display": _fmt_decimal(total_value, 2),
            "total_profit_display": _fmt_decimal(total_profit, 2),
            "total_profit_class": _value_class(total_profit),
            "managed_profit_display": _fmt_decimal(managed_profit, 2),
            "managed_profit_class": _value_class(managed_profit),
            "active_cycle_count": active_cycle_count,
            "can_sell_all": any(item["can_sell"] for item in snapshot_rows),
            "portfolio_value_target_display": portfolio_target_view["target_display"],
            "portfolio_value_target_status_label": portfolio_target_view["status_label"],
            "portfolio_value_target_status_class": portfolio_target_view["status_class"],
            "portfolio_value_target_note": portfolio_target_view["note"],
            "portfolio_value_target_updated_at": portfolio_target_view["updated_at"],
            "portfolio_value_target_triggered_at": portfolio_target_view["triggered_at"],
            "portfolio_value_target_input_value": portfolio_target_view["input_value"],
            "can_clear_portfolio_value_target": portfolio_target_view["can_clear"],
            "warnings": warnings,
            "snapshots": snapshot_rows,
            "cycles": cycle_cards,
            "operations": operations,
            "bitget_enabled": self.settings.bitget_enabled,
            "bitget_orders": self._build_bitget_order_cards(),
        }

    def manual_sell_asset(self, asset: str, restart_cycle: bool = True) -> str:
        normalized_asset = asset.upper()
        snapshots = self.portfolio.build_snapshots()
        snapshot = next((item for item in snapshots if item.asset == normalized_asset), None)
        if snapshot is None or not snapshot.symbol:
            raise RuntimeError(f"No sellable position found for {normalized_asset}.")
        if snapshot.free_quantity <= 0:
            raise RuntimeError(f"{normalized_asset} has no free quantity available to sell.")

        cycle = self.state.get_cycles().get(snapshot.symbol)
        if cycle is None and not self.settings.dry_run:
            open_orders_index = self._get_open_algo_orders_index()
            symbol_orders = open_orders_index["by_symbol"].get(snapshot.symbol, [])
            working_order = next((order for order in symbol_orders if order.get("algoStatus") == "WORKING"), None)
            if working_order is not None:
                cycle = self._merge_remote_order(
                    {"asset": snapshot.asset, "symbol": snapshot.symbol, "algo_status": "WORKING"},
                    working_order,
                )

        return self._profit_exit(snapshot, cycle, reason="manual sell from dashboard", restart_cycle=restart_cycle)

    def manual_sell_all_to_usdt(self) -> str:
        sold_assets, skipped_assets, failures = self._sell_all_to_usdt(
            reason="manual sell all to USDT from dashboard",
            restart_cycle=False,
        )

        if not sold_assets:
            if failures:
                raise RuntimeError("No assets were sold. Failures: " + "; ".join(failures))
            if skipped_assets:
                raise RuntimeError("No assets were sold. " + "; ".join(skipped_assets))
            raise RuntimeError("No sellable non-USDT assets were found.")

        summary = f"Sold {len(sold_assets)} asset(s) to USDT."
        if skipped_assets:
            summary = summary + " Skipped: " + "; ".join(skipped_assets)
        if failures:
            summary = summary + " Failures: " + "; ".join(failures)
        return summary

    def stop_asset_operation(self, asset: str) -> str:
        normalized_asset = asset.upper()
        rule = self.settings.assets.get(normalized_asset)
        if rule is None or not rule.enabled:
            raise RuntimeError(f"{normalized_asset} is not configured as a managed asset.")

        self._set_asset_allowed(normalized_asset, False, "Stopped by user until allowed again")
        symbol = rule.symbol or self.client.find_symbol_for_asset(normalized_asset, self.settings.stable_quote_assets)
        cycle = self.state.get_cycles().get(symbol) if symbol else None

        if cycle is None and symbol and not self.settings.dry_run:
            open_orders_index = self._get_open_algo_orders_index()
            symbol_orders = open_orders_index["by_symbol"].get(symbol, [])
            working_order = next((order for order in symbol_orders if order.get("algoStatus") == "WORKING"), None)
            if working_order is not None:
                cycle = self._merge_remote_order(
                    {"asset": normalized_asset, "symbol": symbol, "algo_status": "WORKING"},
                    working_order,
                )

        cycle_stopped = False
        if cycle:
            self._cancel_open_algo_order(cycle, reason="operation stopped by user")
            cycle_stopped = True
            if symbol:
                self.state.delete_cycle(symbol)

        snapshot = self._get_latest_snapshot(normalized_asset, symbol)
        if snapshot is not None and snapshot.symbol and snapshot.free_quantity > 0:
            sale_message = self._profit_exit(snapshot, None, reason="operation stopped by user", restart_cycle=False)
            return f"{normalized_asset} strategy paused. {sale_message}"

        if cycle_stopped:
            return f"{normalized_asset} strategy paused and its TWAP was stopped. No free position remained to sell."

        return f"{normalized_asset} strategy paused. No active TWAP or free position needed to be sold."

    def start_asset_operation(self, asset: str) -> str:
        normalized_asset = asset.upper()
        rule = self.settings.assets.get(normalized_asset)
        if rule is None or not rule.enabled:
            raise RuntimeError(f"{normalized_asset} is not configured as a managed asset.")

        self._set_asset_allowed(normalized_asset, True, "Started by user")
        symbol = rule.symbol or self.client.find_symbol_for_asset(normalized_asset, self.settings.stable_quote_assets)
        if symbol is None:
            return f"{normalized_asset} strategy was allowed, but no supported stablecoin symbol was found."

        if not self.settings.dry_run:
            open_orders_index = self._get_open_algo_orders_index()
            if self._has_open_algo_order(symbol, open_orders_index):
                return f"{normalized_asset} strategy is already running on Binance for {symbol}."

        existing_cycle = self.state.get_cycles().get(symbol)
        if existing_cycle is not None and not self._should_restart_cycle(existing_cycle):
            return f"{normalized_asset} strategy is already tracked with status {existing_cycle.get('algo_status', 'UNKNOWN')}."

        snapshot = self._get_latest_snapshot(normalized_asset, symbol)
        if not self._is_asset_flat(snapshot, symbol):
            holding_value = snapshot.value_usd if snapshot and snapshot.value_usd is not None else Decimal("0")
            return (
                f"{normalized_asset} strategy was allowed, but no new TWAP was started because {symbol} "
                f"still has an open position worth about ${_fmt_decimal(holding_value, 2)}."
            )

        self._start_cycle(normalized_asset, symbol, rule)
        return f"{normalized_asset} strategy started for {symbol}."

    def allow_asset_operation(self, asset: str, start_now: bool = True) -> str:
        normalized_asset = asset.upper()
        rule = self.settings.assets.get(normalized_asset)
        if rule is None or not rule.enabled:
            raise RuntimeError(f"{normalized_asset} is not configured as a managed asset.")

        if start_now:
            return self.start_asset_operation(normalized_asset)

        self._set_asset_allowed(normalized_asset, True, "Allowed by user")
        symbol = rule.symbol or self.client.find_symbol_for_asset(normalized_asset, self.settings.stable_quote_assets)
        if not start_now:
            return f"{normalized_asset} strategy allowed."
        if symbol is None:
            return f"{normalized_asset} strategy allowed, but no supported stablecoin symbol was found."

        if not self.settings.dry_run:
            open_orders_index = self._get_open_algo_orders_index()
            if self._has_open_algo_order(symbol, open_orders_index):
                return f"{normalized_asset} strategy allowed. Binance already has an open Spot Algo Order for {symbol}."

        existing_cycle = self.state.get_cycles().get(symbol)
        if existing_cycle is not None and not self._should_restart_cycle(existing_cycle):
            return f"{normalized_asset} strategy allowed. Existing cycle status is {existing_cycle.get('algo_status', 'UNKNOWN')}."

        self._start_cycle(normalized_asset, symbol, rule)
        return f"{normalized_asset} strategy allowed and a new TWAP was started for {symbol}."

    def set_portfolio_value_target(self, raw_value: str) -> str:
        value_text = str(raw_value).strip()
        if not value_text:
            raise RuntimeError("Portfolio liquidation target must be provided in USD.")

        try:
            target_value = Decimal(value_text)
        except Exception as exc:
            raise RuntimeError("Portfolio liquidation target must be a valid USD number.") from exc

        if target_value <= 0:
            raise RuntimeError("Portfolio liquidation target must be greater than zero.")

        target_state = {
            "value_usd": str(target_value),
            "active": True,
            "updated_at": iso_now(),
            "triggered_at": None,
        }
        self.state.set_portfolio_value_target(target_state)

        snapshots = self.portfolio.build_snapshots()
        total_value = self._portfolio_total_value(snapshots)
        message = (
            f"Portfolio liquidation target armed at ${_fmt_decimal(target_value, 2)}. "
            f"Current portfolio value is ${_fmt_decimal(total_value, 2)}."
        )
        LOGGER.info(message)
        self.notifier.send(message)

        if total_value >= target_value:
            liquidation_summary = self._portfolio_value_liquidation_exit(
                snapshots,
                reason="portfolio value target reached",
                target_value=target_value,
            )
            return f"{message} {liquidation_summary}"

        return message

    def set_whole_strategy_profit_target(self, raw_value: str) -> str:
        value_text = str(raw_value).strip()
        if not value_text:
            raise RuntimeError("Whole strategy trigger must be provided in USD.")

        try:
            target_value = Decimal(value_text)
        except Exception as exc:
            raise RuntimeError("Whole strategy trigger must be a valid USD number.") from exc

        if target_value <= 0:
            raise RuntimeError("Whole strategy trigger must be greater than zero.")

        target_state = {
            "value_usd": str(target_value),
            "updated_at": iso_now(),
        }
        self.state.set_whole_strategy_profit_target(target_state)

        snapshots = self.portfolio.build_snapshots()
        managed_profit = sum(
            (snapshot.profit_usd or Decimal("0"))
            for snapshot in snapshots
            if snapshot.asset in self.settings.assets and snapshot.profit_usd is not None
        )
        message = (
            f"Whole strategy profit trigger set to ${_fmt_decimal(target_value, 2)}. "
            f"Current managed PnL is ${_fmt_decimal(managed_profit, 2)}."
        )
        LOGGER.info(message)
        self.notifier.send(message)

        if self._whole_strategy_target_reached(snapshots):
            exit_summary = self._whole_strategy_exit(snapshots, reason="whole strategy profit target reached")
            return f"{message} {exit_summary}"

        return message

    def clear_whole_strategy_profit_target(self) -> str:
        target_state = self.state.get_whole_strategy_profit_target()
        if target_state is None:
            return (
                "Whole strategy profit trigger already uses the strategy.json default of "
                f"${_fmt_decimal(self.settings.profit_target_usd, 2)}."
            )

        self.state.clear_whole_strategy_profit_target()
        message = (
            "Whole strategy profit trigger reset to the strategy.json default of "
            f"${_fmt_decimal(self.settings.profit_target_usd, 2)}."
        )
        LOGGER.info(message)
        self.notifier.send(message)
        return message

    def clear_portfolio_value_target(self) -> str:
        target_state = self.state.get_portfolio_value_target()
        if target_state is None:
            return "No portfolio liquidation target is set."

        self.state.clear_portfolio_value_target()
        message = "Portfolio liquidation target cleared."
        LOGGER.info(message)
        self.notifier.send(message)
        return message

    def _portfolio_total_value(self, snapshots: list[AssetSnapshot]) -> Decimal:
        return sum((snapshot.value_usd or Decimal("0") for snapshot in snapshots), Decimal("0"))

    def _has_meaningful_position(self, snapshot: AssetSnapshot | None, symbol: str | None) -> bool:
        if snapshot is None:
            return False
        if snapshot.free_quantity <= 0 and snapshot.locked_quantity <= 0 and snapshot.quantity <= 0:
            return False
        if symbol:
            try:
                info = self.client.get_symbol(symbol)
            except Exception:
                info = None
            else:
                if snapshot.value_usd is not None and info.min_notional > 0 and snapshot.value_usd >= info.min_notional:
                    return True
                if info.min_notional <= 0 and snapshot.quantity >= info.min_qty:
                    return True
        if snapshot.value_usd is not None:
            return snapshot.value_usd > Decimal("1")
        return snapshot.quantity > Decimal("0")

    def _is_asset_flat(self, snapshot: AssetSnapshot | None, symbol: str | None) -> bool:
        return not self._has_meaningful_position(snapshot, symbol)

    @staticmethod
    def _parse_whole_strategy_profit_target_amount(target_state: dict | None) -> Decimal | None:
        if not isinstance(target_state, dict):
            return None
        raw_value = target_state.get("value_usd")
        if raw_value is None or raw_value == "":
            return None
        try:
            value = Decimal(str(raw_value))
        except Exception:
            return None
        if value <= 0:
            return None
        return value

    def _whole_strategy_profit_target(self) -> Decimal:
        target_value = self._parse_whole_strategy_profit_target_amount(self.state.get_whole_strategy_profit_target())
        if target_value is None:
            return self.settings.profit_target_usd
        return target_value

    def _whole_strategy_profit_target_pct(self) -> Decimal:
        if self.settings.profit_target_pct <= 0:
            return Decimal("0")
        return self.settings.profit_target_pct

    def _build_whole_strategy_profit_target_view(self) -> dict[str, object]:
        target_state = self.state.get_whole_strategy_profit_target()
        target_value = self._parse_whole_strategy_profit_target_amount(target_state)
        effective_target = target_value if target_value is not None else self.settings.profit_target_usd
        has_override = target_value is not None
        percent_note = (
            f" The basket also requires at least {_fmt_decimal(self._whole_strategy_profit_target_pct(), 2)}% profit."
            if self._whole_strategy_profit_target_pct() > 0
            else ""
        )
        return {
            "target_display": _fmt_decimal(effective_target, 2),
            "input_value": _fmt_decimal(effective_target, 2),
            "default_display": _fmt_decimal(self.settings.profit_target_usd, 2),
            "source_label": "CUSTOM" if has_override else "CONFIG",
            "source_class": "pill-teal" if has_override else "pill-slate",
            "updated_at": str(target_state.get("updated_at") or "-") if isinstance(target_state, dict) else "-",
            "note": (
                "Using a runtime override from the dashboard."
                + percent_note
                if has_override
                else "Using the strategy.json default. Set a runtime value here to change the managed basket profit trigger without editing strategy.json or restarting the bot."
                + percent_note
            ),
            "can_clear": has_override,
        }

    @staticmethod
    def _parse_portfolio_value_target_amount(target_state: dict | None) -> Decimal | None:
        if not isinstance(target_state, dict):
            return None
        raw_value = target_state.get("value_usd")
        if raw_value is None or raw_value == "":
            return None
        try:
            value = Decimal(str(raw_value))
        except Exception:
            return None
        if value <= 0:
            return None
        return value

    def _build_portfolio_value_target_view(self, total_value: Decimal) -> dict[str, object]:
        target_state = self.state.get_portfolio_value_target()
        target_value = self._parse_portfolio_value_target_amount(target_state)
        if target_value is None:
            return {
                "target_display": "-",
                "status_label": "OFF",
                "status_class": _portfolio_target_status_class("OFF"),
                "note": "Set a portfolio value here. When total portfolio value reaches it, the bot will pause managed assets, cancel managed TWAPs, and sell every sellable non-USDT asset into USDT.",
                "updated_at": "-",
                "triggered_at": "-",
                "input_value": "",
                "can_clear": False,
            }

        is_active = bool(target_state.get("active", True)) if target_state else False
        if is_active:
            gap = target_value - total_value
            if gap > 0:
                note = f"Armed. Remaining gap: ${_fmt_decimal(gap, 2)} before full liquidation."
            else:
                note = "This target is already at or below current portfolio value. The next evaluation will liquidate immediately."
            status_label = "ACTIVE"
        else:
            note = "This target has already triggered and is now inactive. Set a new value to arm it again."
            status_label = "TRIGGERED"

        return {
            "target_display": _fmt_decimal(target_value, 2),
            "status_label": status_label,
            "status_class": _portfolio_target_status_class(status_label),
            "note": note,
            "updated_at": str(target_state.get("updated_at") or "-"),
            "triggered_at": str(target_state.get("triggered_at") or "-"),
            "input_value": f"{target_value:.2f}",
            "can_clear": True,
        }

    def _build_portfolio_value_target_status_line(self, total_value: Decimal) -> str:
        target_state = self.state.get_portfolio_value_target()
        target_value = self._parse_portfolio_value_target_amount(target_state)
        if target_value is None:
            return "PortfolioStop=OFF"

        is_active = bool(target_state.get("active", True)) if target_state else False
        if is_active:
            gap = target_value - total_value
            return f"PortfolioStop=ACTIVE @${_fmt_decimal(target_value, 2)} | Gap=${_fmt_decimal(gap, 2)}"

        triggered_at = str(target_state.get("triggered_at") or "-") if target_state else "-"
        return f"PortfolioStop=TRIGGERED @${_fmt_decimal(target_value, 2)} | TriggeredAt={triggered_at}"

    def _portfolio_value_target_reached(self, snapshots: list[AssetSnapshot]) -> bool:
        target_state = self.state.get_portfolio_value_target()
        target_value = self._parse_portfolio_value_target_amount(target_state)
        if target_value is None:
            return False
        if not bool(target_state.get("active", True)):
            return False
        return self._portfolio_total_value(snapshots) >= target_value

    def _sell_all_to_usdt(
        self,
        reason: str,
        restart_cycle: bool = False,
    ) -> tuple[list[str], list[str], list[str]]:
        snapshots = self.portfolio.build_snapshots()
        cycles = self.state.get_cycles()
        sold_assets: list[str] = []
        skipped_assets: list[str] = []
        failures: list[str] = []

        for snapshot in snapshots:
            if snapshot.asset == "USDT":
                continue
            if snapshot.free_quantity <= 0:
                continue

            usdt_symbol = self._resolve_usdt_sale_symbol(snapshot.asset, snapshot.symbol)
            if usdt_symbol is None:
                skipped_assets.append(f"{snapshot.asset}: no direct USDT pair")
                continue

            try:
                price_usd = snapshot.price_usd if snapshot.symbol == usdt_symbol else self.client.get_price(usdt_symbol)
                cycle = cycles.get(usdt_symbol)
                sale_snapshot = AssetSnapshot(
                    asset=snapshot.asset,
                    symbol=usdt_symbol,
                    quantity=snapshot.quantity,
                    free_quantity=snapshot.free_quantity,
                    locked_quantity=snapshot.locked_quantity,
                    price_usd=price_usd,
                    value_usd=snapshot.free_quantity * price_usd if price_usd is not None else None,
                    avg_cost_usd=snapshot.avg_cost_usd,
                    cost_basis_usd=snapshot.cost_basis_usd,
                    profit_usd=snapshot.profit_usd,
                    profit_pct=snapshot.profit_pct,
                    note=snapshot.note,
                )
                self._profit_exit(
                    sale_snapshot,
                    cycle,
                    reason=reason,
                    restart_cycle=restart_cycle,
                )
            except Exception as exc:
                failures.append(f"{snapshot.asset}: {exc}")
            else:
                sold_assets.append(snapshot.asset)

        return sold_assets, skipped_assets, failures

    def _portfolio_value_liquidation_exit(
        self,
        snapshots: list[AssetSnapshot],
        reason: str,
        target_value: Decimal | None = None,
    ) -> str:
        current_total_value = self._portfolio_total_value(snapshots)
        target_state = self.state.get_portfolio_value_target() or {}
        effective_target_value = target_value or self._parse_portfolio_value_target_amount(target_state)
        managed_cycles = self.state.get_cycles()
        open_orders_index: dict[str, dict[str, dict] | dict[str, list[dict]]] = {"by_algo_id": {}, "by_client_algo_id": {}, "by_symbol": {}}
        lookup_warning: str | None = None

        if not self.settings.dry_run:
            try:
                open_orders_index = self._get_open_algo_orders_index()
            except RuntimeError as exc:
                lookup_warning = str(exc)

        cycles_to_cancel: dict[str, dict] = {}
        for asset, rule in self.settings.assets.items():
            self._set_asset_allowed(asset, False, "Portfolio value target triggered")
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)
            if not symbol:
                continue

            cycle = managed_cycles.get(symbol)
            if cycle is None and lookup_warning is None and not self.settings.dry_run:
                symbol_orders = open_orders_index["by_symbol"].get(symbol, [])
                working_order = next((order for order in symbol_orders if order.get("algoStatus") == "WORKING"), None)
                if working_order is not None:
                    cycle = self._merge_remote_order(
                        {"asset": asset, "symbol": symbol, "algo_status": "WORKING"},
                        working_order,
                    )
            if cycle is not None:
                cycles_to_cancel[symbol] = cycle

        cancelled_count = 0
        cancel_failures: list[str] = []
        for symbol, cycle in cycles_to_cancel.items():
            try:
                self._cancel_open_algo_order(cycle, reason)
            except Exception as exc:
                cancel_failures.append(f"{symbol}: {exc}")
            else:
                cancelled_count += 1
                self.state.delete_cycle(symbol)

        sold_assets, skipped_assets, sell_failures = self._sell_all_to_usdt(
            reason=reason,
            restart_cycle=False,
        )

        updated_target_state = {
            "value_usd": str(effective_target_value) if effective_target_value is not None else str(target_state.get("value_usd") or ""),
            "active": False,
            "updated_at": str(target_state.get("updated_at") or iso_now()),
            "triggered_at": iso_now(),
            "last_portfolio_value_usd": str(current_total_value),
        }
        self.state.set_portfolio_value_target(updated_target_state)

        summary = (
            f"Portfolio value target reached at ${_fmt_decimal(current_total_value, 2)}. "
            f"Paused {len(self.settings.assets)} managed asset(s), cancelled {cancelled_count} managed TWAP(s), "
            f"and sold {len(sold_assets)} asset(s)."
        )
        if lookup_warning:
            summary = summary + f" Open-order lookup warning: {lookup_warning}"
        if cancel_failures:
            summary = summary + " Cancel failures: " + "; ".join(cancel_failures)
        if skipped_assets:
            summary = summary + " Skipped: " + "; ".join(skipped_assets)
        if sell_failures:
            summary = summary + " Sell failures: " + "; ".join(sell_failures)

        LOGGER.info(summary)
        self.notifier.send(summary)
        return summary

    def _dashboard_access_text(self) -> str:
        if self.settings.dashboard_url:
            return f"Dashboard: {self.settings.dashboard_url}"
        return "Dashboard URL is not configured. Set DASHBOARD_URL or strategy.json dashboard_url after exposing the page."

    def _build_overall_telegram_status(
        self,
        cycles: dict[str, dict],
    ) -> str:
        working_count = 0
        pending_count = 0
        paused_count = 0
        idle_count = 0
        attention_count = 0

        for asset, rule in self.settings.assets.items():
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)
            cycle = cycles.get(symbol) if symbol else None
            if not self._is_asset_allowed(asset):
                paused_count += 1
                continue

            status = str(cycle.get("algo_status", "IDLE")).upper() if cycle else "IDLE"
            if status == "WORKING":
                working_count += 1
                continue
            if status == "SUBMITTED":
                pending_count += 1
                continue
            if status in {"REJECTED", "MISSING", "FAILED", "EXPIRED"}:
                attention_count += 1
                continue
            idle_count += 1

        total_assets = len(self.settings.assets)
        mode = "DRY_RUN" if self.settings.dry_run else "LIVE"
        if attention_count > 0:
            overall = "ATTENTION"
        elif working_count > 0 or pending_count > 0:
            overall = "RUNNING"
        elif paused_count == total_assets and total_assets > 0:
            overall = "PAUSED"
        else:
            overall = "IDLE"

        return (
            f"Overall={overall} | Mode={mode} | Active={working_count} | Pending={pending_count} | "
            f"Idle={idle_count} | Paused={paused_count} | Attention={attention_count}"
        )

    def _build_telegram_status(self) -> str:
        snapshots = self.portfolio.build_snapshots()
        snapshot_by_asset = {snapshot.asset: snapshot for snapshot in snapshots}
        cycles = self.state.get_cycles()
        warnings: list[str] = []
        if not self.settings.dry_run:
            try:
                open_orders_index = self._get_open_algo_orders_index()
            except RuntimeError as exc:
                warnings.append(str(exc))
            else:
                cycles.update(self._adopt_remote_managed_cycles(open_orders_index, cycles))
        total_value = sum((snapshot.value_usd or Decimal("0") for snapshot in snapshots), Decimal("0"))
        managed_profit = sum(
            (snapshot.profit_usd or Decimal("0"))
            for snapshot in snapshots
            if snapshot.asset in self.settings.assets and snapshot.profit_usd is not None
        )

        lines = [
            "Managed asset status:",
            self._build_overall_telegram_status(cycles),
            (
                f"Portfolio=${_fmt_decimal(total_value, 2)} | ManagedPnL=${_fmt_decimal(managed_profit, 2)} | "
                f"WholeTarget=${_fmt_decimal(self._whole_strategy_profit_target(), 2)}"
                + (
                    f" and { _fmt_decimal(self._whole_strategy_profit_target_pct(), 2)}%"
                    if self._whole_strategy_profit_target_pct() > 0
                    else ""
                )
            ),
            self._build_portfolio_value_target_status_line(total_value),
            self._dashboard_access_text(),
        ]

        for warning in warnings:
            lines.append(f"Warning: {warning}")

        for asset, rule in self.settings.assets.items():
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets) or "-"
            snapshot = snapshot_by_asset.get(asset)
            cycle = cycles.get(symbol) if symbol != "-" else None
            allowed = "ALLOWED" if self._is_asset_allowed(asset) else "PAUSED"
            cycle_status = str(cycle.get("algo_status", "NONE")) if cycle else "NONE"
            free_qty = _fmt_decimal(snapshot.free_quantity if snapshot else Decimal("0"), 8)
            value = _fmt_decimal(snapshot.value_usd if snapshot else Decimal("0"), 2)
            budget = _fmt_decimal(rule.reentry_quote_budget_usd or self.settings.default_reentry_quote_budget_usd, 2)
            lines.append(f"{asset} {symbol}: {allowed} | cycle={cycle_status} | free={free_qty} | value=${value} | budget=${budget}")

        return "\n".join(lines)

    def run_forever(self) -> None:
        startup_message = (
            f"Binance profit-cycle bot started. dry_run={self.settings.dry_run} "
            f"whole_strategy_profit_target_usd={self._whole_strategy_profit_target()} "
            f"whole_strategy_profit_target_pct={self._whole_strategy_profit_target_pct()}. {self._dashboard_access_text()}"
        )
        LOGGER.info(startup_message)
        self.notifier.send(startup_message)
        while True:
            try:
                self.sync_once()
            except Exception as exc:
                LOGGER.exception("Strategy iteration failed")
                self.notifier.send(f"Bot error: {exc}")
            time.sleep(self.settings.poll_interval_seconds)

    def sync_once(self) -> list[AssetSnapshot]:
        self._process_telegram_commands()
        snapshots = self.portfolio.build_snapshots()
        self._record_managed_profit_history(snapshots)
        open_orders_index = self._get_open_algo_orders_index()
        cycles = self.state.get_cycles()
        cycles.update(self._adopt_remote_managed_cycles(open_orders_index, cycles))

        for symbol, cycle in list(self.state.get_cycles().items()):
            cycle = self._refresh_cycle(symbol, cycle, open_orders_index)
            self._notify_new_sub_orders(symbol, cycle)

        snapshots = self.portfolio.build_snapshots()
        if self._portfolio_value_target_reached(snapshots):
            self._portfolio_value_liquidation_exit(snapshots, reason="portfolio value target reached")
            snapshots = self.portfolio.build_snapshots()
            LOGGER.info("Portfolio summary\n%s", render_snapshots(snapshots))
            return snapshots
        if self._whole_strategy_target_reached(snapshots):
            self._whole_strategy_exit(snapshots, reason="whole strategy profit target reached")
            snapshots = self.portfolio.build_snapshots()
            LOGGER.info("Portfolio summary\n%s", render_snapshots(snapshots))
            return snapshots

        for snapshot in snapshots:
            if not snapshot.symbol or snapshot.asset in self.settings.exclude_assets:
                continue
            if snapshot.asset in self.settings.assets:
                continue
            if snapshot.quantity <= 0 or snapshot.symbol in self.state.get_cycles():
                continue
            if self._target_reached(snapshot, None):
                self._profit_exit(snapshot, None, reason="profit target reached on unmanaged holding")
                snapshots = self.portfolio.build_snapshots()

        open_orders_index = self._get_open_algo_orders_index()
        self._ensure_configured_cycles_running(snapshots, self.state.get_cycles(), open_orders_index)
        snapshots = self.portfolio.build_snapshots()
        LOGGER.info("Portfolio summary\n%s", render_snapshots(snapshots))
        return snapshots

    def _record_managed_profit_history(self, snapshots: list[AssetSnapshot]) -> None:
        snapshot_by_asset = {snapshot.asset: snapshot for snapshot in snapshots}
        recorded_at = iso_now()
        for asset in self.settings.assets:
            snapshot = snapshot_by_asset.get(asset)
            profit_usd = snapshot.profit_usd if snapshot and snapshot.profit_usd is not None else Decimal("0")
            self.state.append_profit_sample(
                asset,
                {
                    "recorded_at": recorded_at,
                    "profit_usd": str(profit_usd),
                },
                limit=PROFIT_HISTORY_LIMIT,
            )

    def _seed_managed_profit_history(
        self,
        snapshots: list[AssetSnapshot],
        profit_history_by_asset: dict[str, list[dict]],
    ) -> None:
        snapshot_by_asset = {snapshot.asset: snapshot for snapshot in snapshots}
        recorded_at = iso_now()
        for asset in self.settings.assets:
            if profit_history_by_asset.get(asset):
                continue
            snapshot = snapshot_by_asset.get(asset)
            profit_usd = snapshot.profit_usd if snapshot and snapshot.profit_usd is not None else Decimal("0")
            sample = {
                "recorded_at": recorded_at,
                "profit_usd": str(profit_usd),
            }
            self.state.append_profit_sample(asset, sample, limit=PROFIT_HISTORY_LIMIT)
            profit_history_by_asset[asset] = [sample]

    def _ensure_configured_cycles_running(
        self,
        snapshots: list[AssetSnapshot],
        cycles: dict[str, dict],
        open_orders_index: dict[str, dict[str, dict] | dict[str, list[dict]]],
    ) -> None:
        snapshot_by_asset = {snapshot.asset: snapshot for snapshot in snapshots}
        for asset, rule in self.settings.assets.items():
            if not rule.enabled or not rule.start_cycle_when_flat:
                continue
            if not self._is_asset_allowed(asset):
                continue
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)
            if not symbol:
                continue
            snapshot = snapshot_by_asset.get(asset)
            if not self._is_asset_flat(snapshot, symbol):
                LOGGER.info("Skipping new TWAP for %s because the managed asset is not flat yet", symbol)
                continue
            if self._has_open_algo_order(symbol, open_orders_index):
                LOGGER.info("Skipping new TWAP for %s because Binance already has an open Spot Algo Order", symbol)
                continue
            existing_cycle = cycles.get(symbol)
            if existing_cycle is not None and self._should_retry_rejected_cycle(existing_cycle, rule):
                self.state.delete_cycle(symbol)
                existing_cycle = None
            if existing_cycle is not None and not self._should_restart_cycle(existing_cycle):
                continue
            try:
                self._start_cycle(asset, symbol, rule)
            except Exception:
                LOGGER.exception("Failed to start managed cycle for %s", symbol)
                continue

    def _target_reached(self, snapshot: AssetSnapshot, cycle: dict | None) -> bool:
        if snapshot.profit_usd is None:
            return False
        target = self._profit_target_for_asset(snapshot.asset, cycle)
        if snapshot.profit_usd < target:
            return False
        target_pct = self._profit_target_pct_for_asset(snapshot.asset, cycle)
        if target_pct <= 0:
            return True
        if snapshot.profit_pct is None:
            return False
        return snapshot.profit_pct >= target_pct

    def _whole_strategy_target_reached(self, snapshots: list[AssetSnapshot]) -> bool:
        managed_profit = Decimal("0")
        managed_cost_basis = Decimal("0")
        has_managed_participant = False
        for snapshot in snapshots:
            if snapshot.asset not in self.settings.assets:
                continue
            if snapshot.profit_usd is not None:
                managed_profit += snapshot.profit_usd
            if snapshot.cost_basis_usd is not None:
                managed_cost_basis += snapshot.cost_basis_usd
            if snapshot.quantity > 0 or snapshot.free_quantity > 0:
                has_managed_participant = True

        if not has_managed_participant:
            has_managed_participant = any(
                str(cycle.get("asset", "")).upper() in self.settings.assets
                for cycle in self.state.get_cycles().values()
            )

        if not has_managed_participant or managed_profit < self._whole_strategy_profit_target():
            return False
        target_pct = self._whole_strategy_profit_target_pct()
        if target_pct <= 0:
            return True
        if managed_cost_basis <= 0:
            return False
        managed_profit_pct = managed_profit / managed_cost_basis * Decimal("100")
        return managed_profit_pct >= target_pct

    def _build_profit_chart(
        self,
        history_samples: list[dict],
        current_profit: Decimal,
        target_profit: Decimal,
    ) -> dict[str, object]:
        width = 320
        height = 132
        left = 10.0
        right = 310.0
        top = 12.0
        bottom = 118.0
        drawable_width = right - left
        drawable_height = bottom - top

        values: list[Decimal] = []
        for sample in history_samples[-PROFIT_HISTORY_LIMIT:]:
            if not isinstance(sample, dict):
                continue
            raw_profit = sample.get("profit_usd")
            if raw_profit is None:
                continue
            try:
                values.append(Decimal(str(raw_profit)))
            except Exception:
                continue

        if not values or values[-1] != current_profit:
            values.append(current_profit)

        if len(values) == 1:
            values.append(values[0])

        range_floor = min(min(values), target_profit, Decimal("0"))
        range_ceiling = max(max(values), target_profit, Decimal("0"))
        if range_floor == range_ceiling:
            padding = abs(range_ceiling) * Decimal("0.25") if range_ceiling != 0 else Decimal("1")
        else:
            padding = (range_ceiling - range_floor) * Decimal("0.12")
        range_floor -= padding
        range_ceiling += padding
        span = range_ceiling - range_floor

        def y_for(value: Decimal) -> float:
            normalized = float((value - range_floor) / span) if span != 0 else 0.5
            return bottom - (normalized * drawable_height)

        points: list[tuple[float, float]] = []
        total_points = len(values)
        for index, value in enumerate(values):
            x = left + (drawable_width * index / (total_points - 1))
            points.append((x, y_for(value)))

        line_points = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
        area_points = f"{points[0][0]:.1f},{bottom:.1f} {line_points} {points[-1][0]:.1f},{bottom:.1f}"

        return {
            "view_box": f"0 0 {width} {height}",
            "left_x": left,
            "right_x": right,
            "zero_y": round(y_for(Decimal("0")), 1),
            "target_y": round(y_for(target_profit), 1),
            "line_points": line_points,
            "area_points": area_points,
            "last_x": round(points[-1][0], 1),
            "last_y": round(points[-1][1], 1),
            "line_class": _value_class(current_profit),
            "min_label": _fmt_decimal(range_floor, 2),
            "max_label": _fmt_decimal(range_ceiling, 2),
        }

    def _build_bitget_order_cards(self) -> list[dict[str, object]]:
        raw_orders = self.state.get_bitget_orders()
        cards: list[dict[str, object]] = []
        for entry in reversed(raw_orders):
            status = str(entry.get("status", "UNKNOWN")).upper()
            if status in {"FILLED", "DRY_RUN"}:
                status_class = "pill-teal"
            elif status == "FAILED":
                status_class = "pill-red"
            else:
                status_class = "pill-slate"
            side = str(entry.get("side", "-")).upper()
            cards.append({
                "symbol": entry.get("symbol", "-"),
                "side": side,
                "side_class": "bitget-buy" if side == "BUY" else "bitget-sell",
                "quantity": entry.get("quantity", "-"),
                "status": status,
                "status_class": status_class,
                "reason": entry.get("reason", "-"),
                "error": entry.get("error", ""),
                "order_id": entry.get("order_id", "-"),
                "created_at": entry.get("created_at", "-"),
            })
        return cards

    def _build_operation_budget_ring(
        self,
        cycle: dict | None,
        rule: AssetRule,
        allowed: bool,
    ) -> dict[str, object]:
        radius = 44
        circumference = 2 * 3.141592653589793 * radius
        cycle_status = str(cycle.get("algo_status", "NONE")).upper() if cycle else "NONE"
        total_budget = Decimal(str(cycle.get("total_budget_usd", "0"))) if cycle else (rule.reentry_quote_budget_usd or self.settings.default_reentry_quote_budget_usd)
        if total_budget <= 0:
            total_budget = rule.reentry_quote_budget_usd or self.settings.default_reentry_quote_budget_usd
        spent_budget = Decimal(str(cycle.get("executed_amt", "0"))) if cycle else Decimal("0")
        spent_budget = max(Decimal("0"), min(spent_budget, total_budget))
        remaining_budget = max(total_budget - spent_budget, Decimal("0"))
        remaining_pct_decimal = ((remaining_budget / total_budget) * Decimal("100")) if total_budget > 0 else Decimal("0")
        remaining_pct_float = float(remaining_pct_decimal)
        stroke_offset = circumference * (1 - max(0.0, min(remaining_pct_float, 100.0)) / 100.0)

        if cycle_status in {"WORKING", "SUBMITTED"}:
            return {
                "active": True,
                "state_class": "operation-budget-pending" if cycle_status == "SUBMITTED" else "operation-budget-active",
                "label": "Budget left",
                "value_display": _fmt_decimal(remaining_budget, 2),
                "note": f"Spent ${_fmt_decimal(spent_budget, 2)} of ${_fmt_decimal(total_budget, 2)}",
                "remaining_budget_display": _fmt_decimal(remaining_budget, 2),
                "spent_budget_display": _fmt_decimal(spent_budget, 2),
                "total_budget_display": _fmt_decimal(total_budget, 2),
                "remaining_pct_display": _fmt_decimal(remaining_pct_decimal, 1),
                "progress_pct": round(remaining_pct_float, 2),
                "radius": radius,
                "circumference": round(circumference, 2),
                "stroke_offset": round(stroke_offset, 2),
            }

        if not allowed:
            return {
                "active": False,
                "state_class": "operation-budget-paused",
                "label": "Budget left",
                "value_display": _fmt_decimal(total_budget, 2),
                "note": "Paused. No budget is being consumed.",
                "remaining_budget_display": _fmt_decimal(total_budget, 2),
                "spent_budget_display": _fmt_decimal(Decimal("0"), 2),
                "total_budget_display": _fmt_decimal(total_budget, 2),
                "remaining_pct_display": _fmt_decimal(Decimal("100"), 1),
                "progress_pct": 100.0,
                "radius": radius,
                "circumference": round(circumference, 2),
                "stroke_offset": 0.0,
            }

        if cycle_status in {"REJECTED", "MISSING", "FAILED", "EXPIRED"}:
            return {
                "active": False,
                "state_class": "operation-budget-alert",
                "label": cycle_status,
                "value_display": _fmt_decimal(total_budget, 2),
                "note": "No active TWAP is consuming this asset budget.",
                "remaining_budget_display": _fmt_decimal(total_budget, 2),
                "spent_budget_display": _fmt_decimal(Decimal("0"), 2),
                "total_budget_display": _fmt_decimal(total_budget, 2),
                "remaining_pct_display": _fmt_decimal(Decimal("100"), 1),
                "progress_pct": 100.0,
                "radius": radius,
                "circumference": round(circumference, 2),
                "stroke_offset": 0.0,
            }

        if cycle_status in {"FINISHED", "CANCELLED"}:
            return {
                "active": False,
                "state_class": "operation-budget-done",
                "label": cycle_status,
                "value_display": _fmt_decimal(remaining_budget, 2),
                "note": f"Last TWAP used ${_fmt_decimal(spent_budget, 2)} of ${_fmt_decimal(total_budget, 2)}.",
                "remaining_budget_display": _fmt_decimal(remaining_budget, 2),
                "spent_budget_display": _fmt_decimal(spent_budget, 2),
                "total_budget_display": _fmt_decimal(total_budget, 2),
                "remaining_pct_display": _fmt_decimal(remaining_pct_decimal, 1),
                "progress_pct": round(remaining_pct_float, 2),
                "radius": radius,
                "circumference": round(circumference, 2),
                "stroke_offset": round(stroke_offset, 2),
            }

        return {
            "active": False,
            "state_class": "operation-budget-idle",
            "label": "Budget left",
            "value_display": _fmt_decimal(total_budget, 2),
            "note": "No active TWAP yet. Full budget is still available.",
            "remaining_budget_display": _fmt_decimal(total_budget, 2),
            "spent_budget_display": _fmt_decimal(Decimal("0"), 2),
            "total_budget_display": _fmt_decimal(total_budget, 2),
            "remaining_pct_display": _fmt_decimal(Decimal("100"), 1),
            "progress_pct": 100.0,
            "radius": radius,
            "circumference": round(circumference, 2),
            "stroke_offset": 0.0,
        }

    def _whole_strategy_exit(self, snapshots: list[AssetSnapshot], reason: str) -> str:
        snapshot_by_asset = {snapshot.asset: snapshot for snapshot in snapshots}
        open_orders_index = self._get_open_algo_orders_index()
        stored_cycles = self.state.get_cycles()
        participants: list[tuple[str, str | None, AssetRule, dict | None]] = []

        for asset, rule in self.settings.assets.items():
            symbol = rule.symbol or self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)
            cycle = stored_cycles.get(symbol) if symbol else None
            if cycle is None and symbol and not self.settings.dry_run:
                symbol_orders = open_orders_index["by_symbol"].get(symbol, [])
                working_order = next((order for order in symbol_orders if order.get("algoStatus") == "WORKING"), None)
                if working_order is not None:
                    cycle = self._merge_remote_order(
                        {"asset": asset, "symbol": symbol, "algo_status": "WORKING"},
                        working_order,
                    )

            snapshot = snapshot_by_asset.get(asset)
            has_position = bool(snapshot and snapshot.symbol and snapshot.quantity > 0)
            if cycle is None and not has_position:
                continue
            participants.append((asset, symbol, rule, cycle))

        if not participants:
            return "No managed strategies were active when the whole-strategy profit target was reached."

        for _, symbol, _, cycle in participants:
            if cycle is not None:
                self._cancel_open_algo_order(cycle, reason)
            if symbol:
                self.state.delete_cycle(symbol)

        sold_assets: list[str] = []
        latest_snapshots = self.portfolio.build_snapshots()
        latest_snapshot_by_asset = {snapshot.asset: snapshot for snapshot in latest_snapshots}
        for asset, _, _, _ in participants:
            snapshot = latest_snapshot_by_asset.get(asset)
            if snapshot is None or not snapshot.symbol or snapshot.free_quantity <= 0:
                continue
            self._profit_exit(snapshot, None, reason=reason, restart_cycle=False)
            sold_assets.append(asset)

        restarted_symbols: list[str] = []
        restart_failures: list[str] = []
        for asset, symbol, rule, _ in participants:
            if not symbol or not rule.enabled or not self._is_asset_allowed(asset):
                continue
            try:
                self._start_cycle(asset, symbol, rule)
            except Exception as exc:
                LOGGER.exception("Whole-strategy restart failed for %s", symbol)
                restart_failures.append(f"{symbol}: {exc}")
            else:
                restarted_symbols.append(symbol)

        summary = (
            f"Whole strategy target reached. Sold {len(sold_assets)} managed asset(s). "
            f"Restarted {len(restarted_symbols)} strategy cycle(s)."
        )
        if restart_failures:
            summary = summary + " Restart failures: " + "; ".join(restart_failures)
        LOGGER.info(summary)
        self.notifier.send(summary)
        return summary

    def _profit_target_for_asset(self, asset: str, cycle: dict | None) -> Decimal:
        rule = self.settings.assets.get(asset)
        if rule and rule.profit_target_usd is not None:
            return rule.profit_target_usd
        if asset in self.settings.assets:
            return self._whole_strategy_profit_target()
        if cycle and cycle.get("profit_target_usd") is not None:
            return Decimal(str(cycle["profit_target_usd"]))
        return self.settings.profit_target_usd

    def _profit_target_pct_for_asset(self, asset: str, cycle: dict | None) -> Decimal:
        rule = self.settings.assets.get(asset)
        if rule and rule.profit_target_pct is not None:
            return max(Decimal("0"), rule.profit_target_pct)
        if asset in self.settings.assets:
            return self._whole_strategy_profit_target_pct()
        if cycle and cycle.get("profit_target_pct") is not None:
            return max(Decimal("0"), Decimal(str(cycle["profit_target_pct"])))
        return max(Decimal("0"), self.settings.profit_target_pct)

    def _start_cycle(self, asset: str, symbol: str, rule: AssetRule | None) -> None:
        if self.entry_guard is not None:
            allowed, reason = self.entry_guard(symbol)
            if not allowed:
                raise RuntimeError(reason)
        info = self.client.get_symbol(symbol)
        configured_budget = rule.reentry_quote_budget_usd if rule and rule.reentry_quote_budget_usd is not None else self.settings.default_reentry_quote_budget_usd
        minimum_budget = info.min_notional * Decimal("1.05") if info.min_notional > 0 else configured_budget
        total_budget = max(configured_budget, minimum_budget)
        duration_seconds = self._resolve_twap_duration_seconds(rule)
        price = self.client.get_price(symbol)
        quantity = self.client.validate_quantity(symbol, total_budget / price, price)
        client_algo_id = uuid.uuid4().hex
        started_at = iso_now()
        confirmation_deadline = (utc_now() + timedelta(seconds=self.settings.algo_confirmation_grace_seconds)).isoformat()
        base_cycle = {
            "asset": asset,
            "symbol": symbol,
            "side": "BUY",
            "started_at": started_at,
            "confirmation_deadline": confirmation_deadline,
            "algo_open": False,
            "submitted_qty": str(quantity),
            "executed_qty": "0",
            "executed_amt": "0",
            "avg_price": "0",
            "total_budget_usd": str(total_budget),
            "duration_seconds": duration_seconds,
            "profit_target_usd": str(self._profit_target_for_asset(asset, None)),
            "profit_target_pct": str(self._profit_target_pct_for_asset(asset, None)),
            "notified_sub_orders": [],
        }

        try:
            response = self.client.new_spot_algo_twap(
                symbol=symbol,
                side="BUY",
                quantity=quantity,
                duration_seconds=duration_seconds,
                client_algo_id=client_algo_id,
            )
        except RuntimeError as exc:
            rejected_cycle = {
                **base_cycle,
                "algo_id": None,
                "client_algo_id": client_algo_id,
                "algo_status": "REJECTED",
                "last_submit_response": {"error": str(exc)},
            }
            self.state.upsert_cycle(symbol, rejected_cycle)
            LOGGER.warning(
                "Spot Algo TWAP submission rejected for %s. clientAlgoId=%s error=%s",
                symbol,
                client_algo_id,
                exc,
            )
            self.notifier.send(f"Spot Algo TWAP submission rejected for {symbol}: {exc}")
            raise

        cycle = {
            **base_cycle,
            "algo_id": str(response.get("algoId")) if response.get("algoId") is not None else None,
            "client_algo_id": response.get("clientAlgoId", client_algo_id),
            "algo_status": "DRY_RUN" if self.settings.dry_run else "SUBMITTED",
            "last_submit_response": response,
        }
        if not self.settings.dry_run:
            remote_order = self.client.find_spot_algo_order(symbol=symbol, client_algo_id=cycle["client_algo_id"])
            if remote_order:
                cycle = self._merge_remote_order(cycle, remote_order)
        self.state.upsert_cycle(symbol, cycle)
        LOGGER.info("Spot Algo TWAP submit ack for %s: %s", symbol, response)
        message = (
            f"Binance Spot Algo TWAP started for {symbol}. qty={quantity} duration_seconds={duration_seconds} "
            f"budget_usd~={total_budget} dry_run={self.settings.dry_run} clientAlgoId={cycle['client_algo_id']}"
        )
        if not self.settings.dry_run and cycle.get("algo_status") == "SUBMITTED":
            LOGGER.warning(
                "Spot Algo TWAP submission for %s is not visible in Binance lookups yet. clientAlgoId=%s ack=%s",
                symbol,
                cycle["client_algo_id"],
                response,
            )
        LOGGER.info(message)
        self.notifier.send(message)
        self._mirror_to_bitget(symbol, "BUY", quantity, reason=f"mirror TWAP start for {symbol}")

    def _profit_exit(self, snapshot: AssetSnapshot, cycle: dict | None, reason: str, restart_cycle: bool = True) -> str:
        if not snapshot.symbol or snapshot.price_usd is None:
            raise RuntimeError("A symbol and price are required before selling.")
        latest_snapshot = self._get_latest_snapshot(snapshot.asset, snapshot.symbol)
        if latest_snapshot is not None:
            snapshot = latest_snapshot
        if cycle:
            self._cancel_open_algo_order(cycle, reason)
            latest_snapshot = self._get_latest_snapshot(snapshot.asset, snapshot.symbol)
            if latest_snapshot is not None:
                snapshot = latest_snapshot
        if snapshot.free_quantity <= 0:
            raise RuntimeError(f"{snapshot.symbol} has no free quantity available to sell.")
        sell_quantity = self.client.validate_quantity(snapshot.symbol, snapshot.free_quantity, snapshot.price_usd)
        order = self.client.market_sell(snapshot.symbol, sell_quantity)
        self._mirror_to_bitget(snapshot.symbol, "SELL", sell_quantity, reason=f"mirror sell for {snapshot.symbol}")
        self.portfolio.invalidate_trades(snapshot.symbol)
        self.state.delete_cycle(snapshot.symbol)
        remaining_note = ""
        try:
            remaining_snapshot = self._get_latest_snapshot(snapshot.asset, snapshot.symbol)
        except Exception:
            remaining_snapshot = None
        if remaining_snapshot is not None and remaining_snapshot.free_quantity > 0:
            remaining_note = f" remaining_free_qty={_fmt_decimal(remaining_snapshot.free_quantity, 8)}"
        message = (
            f"Sold {snapshot.asset} via {snapshot.symbol}. qty={sell_quantity} pnl_usd={snapshot.profit_usd} "
            f"reason={reason} dry_run={self.settings.dry_run} status={order.get('status', 'FILLED')}"
            f"{remaining_note}"
        )
        LOGGER.info(message)
        self.notifier.send(message)

        rule = self.settings.assets.get(snapshot.asset)
        if rule is None:
            rule = AssetRule(asset=snapshot.asset, symbol=snapshot.symbol)
        if restart_cycle and rule.enabled and self._is_asset_allowed(snapshot.asset):
            try:
                self._start_cycle(snapshot.asset, snapshot.symbol, rule)
            except Exception as exc:
                LOGGER.exception("Sell completed but TWAP restart failed for %s", snapshot.symbol)
                self.notifier.send(f"Sell completed for {snapshot.symbol}, but TWAP restart failed: {exc}")
                return f"{message}. Restart failed: {exc}"
        return message

    def _resolve_twap_duration_seconds(self, rule: AssetRule | None) -> int:
        if rule and rule.twap_duration_seconds is not None:
            return max(300, rule.twap_duration_seconds)
        if rule and rule.twap_slices is not None and rule.twap_interval_seconds is not None:
            return max(300, rule.twap_slices * rule.twap_interval_seconds)
        return self.settings.default_twap_duration_seconds

    def _resolve_usdt_sale_symbol(self, asset: str, current_symbol: str | None) -> str | None:
        if current_symbol and current_symbol.endswith("USDT"):
            return current_symbol
        symbol = self.client.find_symbol_for_asset(asset, ("USDT",))
        return symbol

    def _get_open_algo_orders_index(self) -> dict[str, dict[str, dict] | dict[str, list[dict]]]:
        if self.settings.dry_run:
            return {"by_algo_id": {}, "by_client_algo_id": {}, "by_symbol": {}}
        orders = self.client.get_spot_algo_open_orders().get("orders", [])
        by_algo_id: dict[str, dict] = {}
        by_client_algo_id: dict[str, dict] = {}
        by_symbol: dict[str, list[dict]] = {}
        for order in orders:
            algo_id = order.get("algoId")
            client_algo_id = order.get("clientAlgoId")
            symbol = order.get("symbol")
            if algo_id is not None:
                by_algo_id[str(algo_id)] = order
            if client_algo_id:
                by_client_algo_id[str(client_algo_id)] = order
            if symbol:
                by_symbol.setdefault(str(symbol), []).append(order)
        return {"by_algo_id": by_algo_id, "by_client_algo_id": by_client_algo_id, "by_symbol": by_symbol}

    @staticmethod
    def _has_open_algo_order(symbol: str, open_orders_index: dict[str, dict[str, dict] | dict[str, list[dict]]]) -> bool:
        symbol_orders = open_orders_index["by_symbol"].get(symbol, [])
        return any(order.get("algoStatus") == "WORKING" for order in symbol_orders)

    def _refresh_cycle(
        self,
        symbol: str,
        cycle: dict,
        open_orders_index: dict[str, dict[str, dict] | dict[str, list[dict]]],
    ) -> dict:
        remote_order: dict | None = None
        algo_id = cycle.get("algo_id")
        client_algo_id = cycle.get("client_algo_id")

        if algo_id is not None:
            remote_order = open_orders_index["by_algo_id"].get(str(algo_id))
        if remote_order is None and client_algo_id:
            remote_order = open_orders_index["by_client_algo_id"].get(str(client_algo_id))

        updated = dict(cycle)
        previous_status = updated.get("algo_status")
        if remote_order is not None:
            updated = self._merge_remote_order(updated, remote_order)
        elif not self.settings.dry_run and previous_status in {"WORKING", "SUBMITTED"}:
            resolved = self.client.find_spot_algo_order(
                symbol=cycle["symbol"],
                client_algo_id=str(client_algo_id) if client_algo_id else None,
                algo_id=int(algo_id) if algo_id is not None else None,
            )
            if resolved is not None:
                updated = self._merge_remote_order(updated, resolved)
            else:
                deadline_raw = updated.get("confirmation_deadline")
                if deadline_raw and utc_now() < parse_iso(str(deadline_raw)):
                    updated["algo_open"] = False
                    updated["algo_status"] = "SUBMITTED"
                else:
                    updated["algo_open"] = False
                    updated["algo_status"] = "MISSING"

        if updated != cycle:
            self.state.upsert_cycle(symbol, updated)
            if previous_status and previous_status != updated.get("algo_status"):
                self.notifier.send(
                    f"Spot Algo TWAP status changed for {symbol}: {previous_status} -> {updated.get('algo_status')}"
                )
        return updated

    @staticmethod
    def _merge_remote_order(cycle: dict, remote_order: dict) -> dict:
        merged = dict(cycle)
        merged["algo_id"] = str(remote_order.get("algoId")) if remote_order.get("algoId") is not None else merged.get("algo_id")
        merged["client_algo_id"] = remote_order.get("clientAlgoId", merged.get("client_algo_id"))
        merged["algo_status"] = remote_order.get("algoStatus", merged.get("algo_status", "UNKNOWN"))
        merged["algo_open"] = merged.get("algo_status") == "WORKING"
        merged["submitted_qty"] = str(remote_order.get("totalQty", merged.get("submitted_qty", "0")))
        merged["executed_qty"] = str(remote_order.get("executedQty", merged.get("executed_qty", "0")))
        merged["executed_amt"] = str(remote_order.get("executedAmt", merged.get("executed_amt", "0")))
        merged["avg_price"] = str(remote_order.get("avgPrice", merged.get("avg_price", "0")))
        if remote_order.get("bookTime") is not None:
            merged["book_time"] = str(remote_order.get("bookTime"))
        if remote_order.get("endTime") is not None:
            merged["end_time"] = str(remote_order.get("endTime"))
        return merged

    def _should_restart_cycle(self, cycle: dict) -> bool:
        status = str(cycle.get("algo_status", "UNKNOWN"))
        if status == "WORKING":
            return False
        if status == "REJECTED":
            return False
        if status == "SUBMITTED":
            deadline_raw = cycle.get("confirmation_deadline")
            if deadline_raw and utc_now() < parse_iso(str(deadline_raw)):
                return False
            return True
        return True

    def _should_retry_rejected_cycle(self, cycle: dict, rule: AssetRule | None) -> bool:
        if str(cycle.get("algo_status", "")).upper() != "REJECTED":
            return False

        configured_budget = rule.reentry_quote_budget_usd if rule and rule.reentry_quote_budget_usd is not None else self.settings.default_reentry_quote_budget_usd
        configured_duration = self._resolve_twap_duration_seconds(rule)
        configured_target = self._profit_target_for_asset(str(cycle.get("asset", "")), None)

        cycle_budget = Decimal(str(cycle.get("total_budget_usd", "0")))
        cycle_duration = int(cycle.get("duration_seconds", 0))
        cycle_target = Decimal(str(cycle.get("profit_target_usd", "0")))

        return cycle_budget != configured_budget or cycle_duration != configured_duration or cycle_target != configured_target

    def _process_telegram_commands(self) -> None:
        if not self.notifier.enabled or not self.notifier.chat_id:
            return

        next_offset = self.state.get_telegram_update_offset()
        updates = self.notifier.get_updates(offset=next_offset, timeout_seconds=0)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1

            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue

            chat = message.get("chat")
            if not isinstance(chat, dict) or str(chat.get("id")) != str(self.notifier.chat_id):
                continue

            text = str(message.get("text") or "").strip()
            if not text:
                continue

            try:
                response = self._handle_telegram_command(text)
            except Exception as exc:
                LOGGER.exception("Telegram command failed: %s", text)
                response = f"Telegram command failed: {exc}"

            if response:
                self.notifier.send(response)

        if next_offset is not None:
            self.state.set_telegram_update_offset(next_offset)

    def _handle_telegram_command(self, text: str) -> str:
        parts = text.strip().split()
        raw_command = parts[0].lower()
        command = raw_command.lstrip("/")
        is_slash_command = raw_command.startswith("/")
        asset = parts[1].upper() if len(parts) > 1 else None
        argument = " ".join(parts[1:]).strip() if len(parts) > 1 else ""

        if command == "help":
            return (
                "Commands: /status, /page, /portfolio <usd>|off, /allow <asset>, /start <asset>, /stop <asset>, /sell <asset>, /help\n"
                + self._dashboard_access_text()
            )

        if command in {"status", "page", "dashboard"}:
            return self._build_telegram_status()

        if command == "portfolio":
            if not argument:
                snapshots = self.portfolio.build_snapshots()
                total_value = self._portfolio_total_value(snapshots)
                return (
                    f"Portfolio=${_fmt_decimal(total_value, 2)} | {self._build_portfolio_value_target_status_line(total_value)}\n"
                    + self._dashboard_access_text()
                )
            if argument.lower() in {"off", "clear", "none"}:
                return self.clear_portfolio_value_target()
            return self.set_portfolio_value_target(argument)

        if command == "start":
            if not asset:
                if is_slash_command:
                    return (
                        "Commands: /status, /page, /portfolio <usd>|off, /allow <asset>, /start <asset>, /stop <asset>, /sell <asset>, /help\n"
                        + self._dashboard_access_text()
                    )
                return "Usage: start <asset>"
            return self.start_asset_operation(asset)

        if command == "allow":
            if not asset:
                return "Usage: allow <asset>"
            return self.allow_asset_operation(asset)

        if command == "stop":
            if not asset:
                return "Usage: stop <asset>"
            return self.stop_asset_operation(asset)

        if command == "sell":
            if not asset:
                return "Usage: sell <asset>"
            return self.manual_sell_asset(asset, restart_cycle=self._is_asset_allowed(asset))

        if is_slash_command:
            return "Unknown command. Use /help."
        return ""

    def _notify_new_sub_orders(self, symbol: str, cycle: dict) -> dict:
        if self.settings.dry_run or not self.settings.notify_on_slice or cycle.get("algo_id") is None:
            return cycle

        sub_orders_result = self.client.get_spot_algo_sub_orders(int(cycle["algo_id"]))
        known_sub_orders = list(cycle.get("notified_sub_orders", []))
        known_sub_order_ids = set(known_sub_orders)
        new_sub_orders: list[dict] = []
        for sub_order in sub_orders_result.get("subOrders", []):
            order_id = str(sub_order.get("orderId"))
            if order_id in known_sub_order_ids:
                continue
            known_sub_orders.append(order_id)
            known_sub_order_ids.add(order_id)
            new_sub_orders.append(sub_order)

        for sub_order in new_sub_orders:
            self.notifier.send(
                f"Spot Algo TWAP sub-order for {symbol}. orderId={sub_order.get('orderId')} "
                f"qty={sub_order.get('executedQty')} amt={sub_order.get('executedAmt')} "
                f"avg_price={sub_order.get('avgPrice')} status={sub_order.get('orderStatus')}"
            )

        updated = dict(cycle)
        updated["notified_sub_orders"] = known_sub_orders
        updated["executed_qty"] = str(sub_orders_result.get("executedQty", updated.get("executed_qty", "0")))
        updated["executed_amt"] = str(sub_orders_result.get("executedAmt", updated.get("executed_amt", "0")))
        if updated != cycle:
            self.state.upsert_cycle(symbol, updated)
        return updated

    def _cancel_open_algo_order(self, cycle: dict, reason: str) -> None:
        if self.settings.dry_run:
            return

        updated = dict(cycle)
        algo_id = updated.get("algo_id")
        client_algo_id = updated.get("client_algo_id")
        resolved = self.client.find_spot_algo_order(
            updated["symbol"],
            client_algo_id=str(client_algo_id) if client_algo_id else None,
            algo_id=int(algo_id) if algo_id is not None else None,
        )
        if resolved is not None:
            updated = self._merge_remote_order(updated, resolved)
            self.state.upsert_cycle(updated["symbol"], updated)
            algo_id = updated.get("algo_id")

        if algo_id is None or str(updated.get("algo_status", "")).upper() != "WORKING":
            return

        self.client.cancel_spot_algo_order(int(algo_id))
        deadline = time.monotonic() + CANCEL_CONFIRMATION_TIMEOUT_SECONDS
        while True:
            resolved = self.client.find_spot_algo_order(
                updated["symbol"],
                client_algo_id=str(updated.get("client_algo_id")) if updated.get("client_algo_id") else None,
                algo_id=int(algo_id),
            )
            if resolved is None:
                updated["algo_status"] = "CANCELLED"
                updated["algo_open"] = False
                break

            updated = self._merge_remote_order(updated, resolved)
            if str(updated.get("algo_status", "")).upper() != "WORKING":
                updated["algo_open"] = False
                break

            if time.monotonic() >= deadline:
                raise RuntimeError(f"Timed out waiting for Binance to stop Spot Algo TWAP for {updated['symbol']}.")
            time.sleep(CANCEL_CONFIRMATION_POLL_SECONDS)

        self.state.upsert_cycle(updated["symbol"], updated)
        self.notifier.send(
            f"Stopped Spot Algo TWAP for {updated['symbol']}. algoId={algo_id} "
            f"final_status={updated.get('algo_status', 'CANCELLED')} reason={reason}"
        )

    def _get_latest_snapshot(self, asset: str, symbol: str | None = None) -> AssetSnapshot | None:
        snapshots = self.portfolio.build_snapshots()
        if symbol:
            snapshot = next((item for item in snapshots if item.symbol == symbol), None)
            if snapshot is not None:
                return snapshot
        return next((item for item in snapshots if item.asset == asset), None)

    @staticmethod
    def _format_submit_response(response: object) -> str:
        if not isinstance(response, dict):
            return "-"
        parts: list[str] = []
        for key in ("error", "code", "msg", "success", "status", "algoId", "clientAlgoId"):
            value = response.get(key)
            if value is None or value == "":
                continue
            parts.append(f"{key}={value}")
        return " | ".join(parts) if parts else "-"

    @staticmethod
    def _seconds_until_retry(cycle: dict, now: datetime) -> int | None:
        if str(cycle.get("algo_status", "")).upper() != "SUBMITTED":
            return None
        deadline_raw = cycle.get("confirmation_deadline")
        if not deadline_raw:
            return None
        try:
            deadline = parse_iso(str(deadline_raw))
        except ValueError:
            return None
        return max(0, int((deadline - now).total_seconds()))

    def _cycle_status_note(self, cycle: dict | None, now: datetime) -> str:
        if not cycle:
            return ""
        status = str(cycle.get("algo_status", "UNKNOWN")).upper()
        if status == "WORKING":
            return "Binance reports this TWAP as working."
        if status == "SUBMITTED":
            retry_in = self._seconds_until_retry(cycle, now)
            if retry_in is None:
                return "Waiting for Binance to surface this submission in open or historical algo lookups."
            if retry_in > 0:
                return f"Waiting for Binance to surface this submission. Auto-retry in {retry_in}s if it stays invisible."
            return "Waiting for the next sync to decide whether this submission is missing."
        if status == "MISSING":
            return "Binance did not expose this submission in open or historical algo lookups. The bot will resubmit."
        if status == "REJECTED":
            response_display = self._format_submit_response(cycle.get("last_submit_response"))
            return f"Binance rejected this TWAP submission. {response_display}"
        if status == "CANCELLED":
            return "This TWAP was cancelled locally after it was resolved on Binance."
        if status == "FINISHED":
            return "Binance reports this TWAP as finished."
        if status == "DRY_RUN":
            return "No live exchange order was submitted in dry-run mode."
        return "Status reported by bot state or Binance lookup results."
