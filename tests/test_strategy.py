from __future__ import annotations

import sys
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.config import AssetRule, Settings
from binance_bot.portfolio import AssetSnapshot
from binance_bot.strategy import TradingStrategy


class FakeClient:
    def __init__(self) -> None:
        self._symbols = {
            "BTCUSDT": SimpleNamespace(
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                min_qty=Decimal("0.00001"),
                step_size=Decimal("0.00001"),
                min_notional=Decimal("10"),
                quote_order_qty_market_allowed=True,
            )
        }

    def find_symbol_for_asset(self, asset: str, stable_quote_assets: tuple[str, ...]) -> str | None:
        if asset.upper() == "BTC":
            return "BTCUSDT"
        return None

    def get_symbol(self, symbol: str):
        return self._symbols[symbol]


class FakePortfolio:
    def __init__(self, snapshots: list[AssetSnapshot]) -> None:
        self.snapshots = list(snapshots)

    def build_snapshots(self) -> list[AssetSnapshot]:
        return list(self.snapshots)

    def invalidate_trades(self, symbol: str) -> None:
        return None


class FakeNotifier:
    enabled = False
    chat_id = None

    def __init__(self) -> None:
        self.messages: list[str] = []

    def send(self, message: str) -> None:
        self.messages.append(message)

    def get_updates(self, offset=None, timeout_seconds=0) -> list[dict]:
        return []


class InMemoryState:
    def __init__(self) -> None:
        self.cycles: dict[str, dict] = {}
        self.asset_controls: dict[str, dict] = {}
        self.profit_history: dict[str, list[dict]] = {}
        self.portfolio_value_target: dict | None = None
        self.whole_strategy_profit_target: dict | None = None
        self.telegram_update_offset: int | None = None
        self.bitget_orders: list[dict] = []

    def get_cycles(self) -> dict[str, dict]:
        return dict(self.cycles)

    def upsert_cycle(self, symbol: str, cycle: dict) -> None:
        self.cycles[symbol] = dict(cycle)

    def delete_cycle(self, symbol: str) -> None:
        self.cycles.pop(symbol, None)

    def get_asset_controls(self) -> dict[str, dict]:
        return dict(self.asset_controls)

    def set_asset_control(self, asset: str, control: dict) -> None:
        self.asset_controls[asset.upper()] = dict(control)

    def delete_asset_control(self, asset: str) -> None:
        self.asset_controls.pop(asset.upper(), None)

    def get_profit_history(self) -> dict[str, list[dict]]:
        return {asset: [dict(item) for item in samples] for asset, samples in self.profit_history.items()}

    def append_profit_sample(self, asset: str, sample: dict, limit: int = 96) -> None:
        asset_key = asset.upper()
        samples = self.profit_history.setdefault(asset_key, [])
        samples.append(dict(sample))
        self.profit_history[asset_key] = samples[-max(2, int(limit)):]

    def get_portfolio_value_target(self) -> dict | None:
        return dict(self.portfolio_value_target) if isinstance(self.portfolio_value_target, dict) else None

    def set_portfolio_value_target(self, target: dict) -> None:
        self.portfolio_value_target = dict(target)

    def clear_portfolio_value_target(self) -> None:
        self.portfolio_value_target = None

    def get_whole_strategy_profit_target(self) -> dict | None:
        return dict(self.whole_strategy_profit_target) if isinstance(self.whole_strategy_profit_target, dict) else None

    def set_whole_strategy_profit_target(self, target: dict) -> None:
        self.whole_strategy_profit_target = dict(target)

    def clear_whole_strategy_profit_target(self) -> None:
        self.whole_strategy_profit_target = None

    def get_telegram_update_offset(self) -> int | None:
        return self.telegram_update_offset

    def set_telegram_update_offset(self, offset: int) -> None:
        self.telegram_update_offset = offset

    def get_bitget_orders(self) -> list[dict]:
        return [dict(order) for order in self.bitget_orders]

    def append_bitget_order(self, order: dict, limit: int = 200) -> None:
        self.bitget_orders.append(dict(order))
        self.bitget_orders = self.bitget_orders[-max(2, int(limit)):]


class RecordingStrategy(TradingStrategy):
    def __init__(self, settings: Settings, client: FakeClient, portfolio: FakePortfolio, notifier: FakeNotifier, state: InMemoryState) -> None:
        super().__init__(settings, client, portfolio, notifier, state)
        self.started: list[tuple[str, str]] = []
        self.cancelled: list[tuple[str, str]] = []
        self.exits: list[tuple[str, str, bool]] = []
        self.whole_exit_calls = 0

    def _start_cycle(self, asset: str, symbol: str, rule: AssetRule | None) -> None:
        self.started.append((asset, symbol))

    def _cancel_open_algo_order(self, cycle: dict, reason: str) -> None:
        self.cancelled.append((cycle["symbol"], reason))

    def _profit_exit(self, snapshot: AssetSnapshot, cycle: dict | None, reason: str, restart_cycle: bool = True) -> str:
        self.exits.append((snapshot.asset, reason, restart_cycle))
        return f"sold {snapshot.asset}"

    def _whole_strategy_exit(self, snapshots: list[AssetSnapshot], reason: str) -> str:
        self.whole_exit_calls += 1
        return "whole strategy exited"


def make_settings(**overrides) -> Settings:
    defaults = Settings(
        root_dir=ROOT_DIR,
        dry_run=True,
        poll_interval_seconds=30,
        profit_target_usd=Decimal("25"),
        profit_target_pct=Decimal("1.5"),
        stable_quote_assets=("USDT",),
        exclude_assets=frozenset({"USDT"}),
        default_reentry_quote_budget_usd=Decimal("500"),
        default_twap_duration_seconds=3600,
        algo_confirmation_grace_seconds=120,
        default_twap_slices=4,
        default_twap_interval_seconds=300,
        notify_on_slice=True,
        api_key="key",
        api_secret="secret",
        telegram_api_key=None,
        telegram_chat_id=None,
        notifications_enabled=False,
        dashboard_url=None,
        state_file=ROOT_DIR / "runtime" / "test-state.json",
        assets={
            "BTC": AssetRule(
                asset="BTC",
                enabled=True,
                symbol="BTCUSDT",
                start_cycle_when_flat=True,
                reentry_quote_budget_usd=Decimal("500"),
            )
        },
        bitget_api_key=None,
        bitget_api_secret=None,
        bitget_passphrase=None,
        bitget_enabled=False,
    )
    return replace(defaults, **overrides)


def make_snapshot(
    *,
    asset: str = "BTC",
    symbol: str = "BTCUSDT",
    quantity: str = "0",
    free_quantity: str = "0",
    locked_quantity: str = "0",
    price_usd: str = "1000",
    value_usd: str = "0",
    avg_cost_usd: str = "1000",
    cost_basis_usd: str = "0",
    profit_usd: str = "0",
    profit_pct: str = "0",
) -> AssetSnapshot:
    return AssetSnapshot(
        asset=asset,
        symbol=symbol,
        quantity=Decimal(quantity),
        free_quantity=Decimal(free_quantity),
        locked_quantity=Decimal(locked_quantity),
        price_usd=Decimal(price_usd),
        value_usd=Decimal(value_usd),
        avg_cost_usd=Decimal(avg_cost_usd),
        cost_basis_usd=Decimal(cost_basis_usd),
        profit_usd=Decimal(profit_usd),
        profit_pct=Decimal(profit_pct),
        note=None,
    )


class StrategyTests(unittest.TestCase):
    def make_strategy(self, settings: Settings, snapshots: list[AssetSnapshot]) -> RecordingStrategy:
        state = InMemoryState()
        return RecordingStrategy(settings, FakeClient(), FakePortfolio(snapshots), FakeNotifier(), state)

    def test_ensure_configured_cycles_running_only_starts_when_flat(self) -> None:
        strategy = self.make_strategy(
            make_settings(),
            [
                make_snapshot(
                    quantity="0.02",
                    free_quantity="0.02",
                    value_usd="40",
                    cost_basis_usd="38",
                    profit_usd="2",
                    profit_pct="5.26",
                )
            ],
        )

        strategy._ensure_configured_cycles_running(
            strategy.portfolio.build_snapshots(),
            strategy.state.get_cycles(),
            {"by_algo_id": {}, "by_client_algo_id": {}, "by_symbol": {}},
        )

        self.assertEqual(strategy.started, [])

    def test_stop_asset_operation_sells_after_pausing(self) -> None:
        strategy = self.make_strategy(
            make_settings(),
            [
                make_snapshot(
                    quantity="0.02",
                    free_quantity="0.02",
                    value_usd="40",
                    cost_basis_usd="38",
                    profit_usd="2",
                    profit_pct="5.26",
                )
            ],
        )
        strategy.state.upsert_cycle("BTCUSDT", {"asset": "BTC", "symbol": "BTCUSDT", "algo_status": "WORKING"})

        message = strategy.stop_asset_operation("BTC")

        self.assertEqual(strategy.cancelled, [("BTCUSDT", "operation stopped by user")])
        self.assertEqual(strategy.exits, [("BTC", "operation stopped by user", False)])
        self.assertEqual(strategy.state.get_cycles(), {})
        self.assertFalse(strategy._is_asset_allowed("BTC"))
        self.assertIn("sold BTC", message)

    def test_whole_strategy_target_requires_profit_percent(self) -> None:
        snapshots = [
            make_snapshot(
                quantity="0.02",
                free_quantity="0.02",
                value_usd="1010",
                cost_basis_usd="1000",
                profit_usd="10",
                profit_pct="1",
            )
        ]
        strategy = self.make_strategy(
            make_settings(
                profit_target_usd=Decimal("5"),
                profit_target_pct=Decimal("2"),
            ),
            snapshots,
        )

        self.assertFalse(strategy._whole_strategy_target_reached(snapshots))

        richer_snapshots = [
            make_snapshot(
                quantity="0.02",
                free_quantity="0.02",
                value_usd="1025",
                cost_basis_usd="1000",
                profit_usd="25",
                profit_pct="2.5",
            )
        ]

        self.assertTrue(strategy._whole_strategy_target_reached(richer_snapshots))

    def test_sync_once_checks_exits_before_starting_new_cycles(self) -> None:
        strategy = self.make_strategy(make_settings(), [])
        strategy._whole_strategy_target_reached = lambda snapshots: True  # type: ignore[method-assign]

        strategy.sync_once()

        self.assertEqual(strategy.whole_exit_calls, 1)
        self.assertEqual(strategy.started, [])


if __name__ == "__main__":
    unittest.main()
