from __future__ import annotations

import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.tenant_fleet import TenantBotFleet
from binance_bot.volatility_bot import VolatilityTwapBot
from binance_bot.dashboard import create_dashboard_app
from binance_bot.saas import SaaSStore
from binance_bot.strategy import TradingStrategy


def _fake_instruments(bot: VolatilityTwapBot, force: bool = False):
    del force
    if bot.primary_exchange == "bitget":
        return {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "api_symbol": "BTCUSDT",
                "baseCoin": "BTC",
                "quoteCoin": "USDT",
                "instrument_type": "crypto",
                "is_reality": False,
            },
            "RNVDAUSDT": {
                "symbol": "RNVDAUSDT",
                "api_symbol": "rNVDAUSDT",
                "baseCoin": "rNVDA",
                "quoteCoin": "USDT",
                "instrument_type": "stock",
                "is_reality": True,
            },
        }
    return {
        "BTCUSDT": {
            "symbol": "BTCUSDT",
            "baseCoin": "BTC",
            "quoteCoin": "USDT",
            "instrument_type": "crypto",
            "is_reality": False,
        },
        "ETHUSDT": {
            "symbol": "ETHUSDT",
            "baseCoin": "ETH",
            "quoteCoin": "USDT",
            "instrument_type": "crypto",
            "is_reality": False,
        },
    }


def _fake_discovery(bot: VolatilityTwapBot, limit: int = 40, force: bool = False):
    del force
    return list(_fake_instruments(bot))[:limit]


def _fake_start(bot: VolatilityTwapBot, budget: float = 5000, duration_hours: float = 12):
    bot.running = True
    bot.state.status = "waiting"
    bot.state.budget_total = Decimal(str(budget))
    bot.state.budget_remaining = Decimal(str(budget))
    return f"Paper bot started for {duration_hours:g}h"


class TenantFleetTests(unittest.TestCase):
    def test_legacy_strategy_checks_emergency_guard_before_exchange_access(self) -> None:
        strategy = object.__new__(TradingStrategy)
        strategy.entry_guard = lambda symbol: (False, f"Emergency lock for {symbol}")

        with self.assertRaisesRegex(RuntimeError, "Emergency lock for BTCUSDT"):
            strategy._start_cycle("BTC", "BTCUSDT", None)

    def test_explicit_tenant_credentials_never_fall_back_to_owner_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "BITGET_AccessAPIKey": "owner-bitget-key",
                "BITGET_secretkey": "owner-bitget-secret",
                "BITGET_PASSWORD": "owner-bitget-passphrase",
                "TELEGRAM_API_KEY": "owner-telegram-token",
                "TELEGRAM_CHAT_ID": "owner-chat-id",
            },
        ):
            bot = VolatilityTwapBot(
                Path(temp_dir),
                "owner-binance-key",
                "owner-binance-secret",
                dry_run=True,
                exchange_credentials={},
            )

            self.assertEqual(bot.api_key, "")
            self.assertEqual(bot.api_secret, "")
            self.assertEqual(bot.bitget_api_key, "")
            self.assertEqual(bot.bitget_api_secret, "")
            self.assertEqual(bot.bitget_passphrase, "")
            self.assertEqual(bot.telegram_api_key, "")
            self.assertEqual(bot.telegram_chat_id, "")
            self.assertFalse(bot.notifications_enabled)
            self.assertFalse(bot.codex_decision_enabled)

    @patch.object(VolatilityTwapBot, "get_discovery_symbols", _fake_discovery)
    @patch.object(VolatilityTwapBot, "get_exchange_instruments", _fake_instruments)
    def test_tenants_have_separate_credentials_paths_and_persisted_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = TenantBotFleet(
                root,
                101,
                exchange_credentials={"binance": {"api_key": "first-key", "api_secret": "first-secret"}},
            )
            second = TenantBotFleet(
                root,
                202,
                exchange_credentials={"binance": {"api_key": "second-key", "api_secret": "second-secret"}},
            )

            first_bot = first.add_pair("BTCUSDT")
            second_bot = second.add_pair("BTCUSDT")
            first_bot.state.position_qty = Decimal("0.25")
            first_bot.state.position_cost_basis = Decimal("100")
            first_bot._save_state()

            self.assertNotEqual(first.root_dir, second.root_dir)
            self.assertNotEqual(first_bot.state_file, second_bot.state_file)
            self.assertEqual(first_bot.api_key, "first-key")
            self.assertEqual(second_bot.api_key, "second-key")
            self.assertFalse(first_bot.notifications_enabled)
            self.assertFalse(first_bot.codex_decision_enabled)
            self.assertEqual(second_bot.state.position_qty, Decimal("0"))

            restored_first = TenantBotFleet(
                root,
                101,
                exchange_credentials={"binance": {"api_key": "first-key", "api_secret": "first-secret"}},
            )
            self.assertEqual(restored_first.get_bot("BTCUSDT").state.position_qty, Decimal("0.25"))
            self.assertEqual(second.get_bot("BTCUSDT").state.position_qty, Decimal("0"))

    @patch.object(VolatilityTwapBot, "get_discovery_symbols", _fake_discovery)
    @patch.object(VolatilityTwapBot, "get_exchange_instruments", _fake_instruments)
    def test_selected_exchange_maps_stock_ticker_and_catalog_labels_asset_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = TenantBotFleet(Path(temp_dir), 303, primary_exchange="binance")
            fleet.set_primary_exchange("bitget")
            stock_bot = fleet.add_pair("NVDA", primary_exchange="bitget")

            self.assertEqual(stock_bot.symbol, "RNVDAUSDT")
            self.assertTrue(stock_bot.is_stock_instrument())
            self.assertTrue(stock_bot.dry_run)
            instruments = fleet.supported_instruments()
            stock = next(item for item in instruments if item["symbol"] == "RNVDAUSDT")
            self.assertEqual(stock["instrument_type"], "stock")
            self.assertTrue(stock["is_stock"])

    @patch.object(VolatilityTwapBot, "get_discovery_symbols", _fake_discovery)
    @patch.object(VolatilityTwapBot, "get_exchange_instruments", _fake_instruments)
    def test_plan_pair_limit_is_enforced_per_tenant(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            fleet = TenantBotFleet(Path(temp_dir), 404, max_pairs=1)
            fleet.add_pair("BTCUSDT")

            with self.assertRaisesRegex(ValueError, "Plan limit reached"):
                fleet.add_pair("ETHUSDT")

    @patch.object(VolatilityTwapBot, "start", _fake_start)
    @patch.object(VolatilityTwapBot, "get_discovery_symbols", _fake_discovery)
    @patch.object(VolatilityTwapBot, "get_exchange_instruments", _fake_instruments)
    def test_http_routes_keep_customer_fleets_and_operator_logs_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "BINANCE_API_KEY": "test-owner-key",
                "BINANCE_API_SECRET": "test-owner-secret",
                "BOT_AUTO_DISCOVERY_ENABLED": "false",
                "BOT_SEED_WORKING_PAIRS": "",
                "BOT_ENFORCE_LOCAL_ACTION_AUTH": "true",
                "BOT_ALLOW_LOCAL_ACTIONS": "false",
                "SAAS_TENANT_ISOLATION_ENABLED": "true",
                "SAAS_PUBLIC_SIGNUP_ENABLED": "false",
                "SAAS_SECURE_COOKIES": "false",
            },
        ):
            root = Path(temp_dir)
            runtime = root / "runtime"
            runtime.mkdir(parents=True)
            (runtime / "managed_pairs.json").write_text(
                '{"default_symbol":"","pairs":[]}',
                encoding="utf-8",
            )
            app = create_dashboard_app(root)
            app.config.update(TESTING=True)
            store = SaaSStore(root)
            first_user = store.create_user("first@example.com", "a-strong-password")
            second_user = store.create_user("second@example.com", "a-strong-password")

            anonymous = app.test_client()
            landing = anonymous.get("/")
            self.assertEqual(landing.status_code, 200)
            self.assertIn(b"customer orders are paper-only", landing.data)
            self.assertIn(b"invite-only", landing.data)
            self.assertEqual(anonymous.get("/signup").status_code, 404)
            denied_local_action = anonymous.post(
                "/api/bot/stop",
                json={"symbol": "BTCUSDT"},
                headers={"X-CSRF-Token": "not-a-session"},
            )
            self.assertEqual(denied_local_action.status_code, 401)

            first_client = app.test_client()
            with first_client.session_transaction() as session:
                session["user_id"] = int(first_user["id"])
                session["csrf_token"] = "first-csrf"
            empty_dashboard = first_client.get("/app/bots/swing")
            self.assertEqual(empty_dashboard.status_code, 200)
            self.assertIn(b"Isolated paper trading", empty_dashboard.data)
            first_add = first_client.post(
                "/api/bot/pairs",
                json={"symbol": "BTCUSDT", "exchange": "binance"},
                headers={"X-CSRF-Token": "first-csrf"},
            )
            self.assertEqual(first_add.status_code, 200, first_add.get_json())
            first_status = first_add.get_json()["status"]
            self.assertTrue(first_status["tenant_isolated"])
            self.assertTrue(first_status["dry_run"])
            self.assertFalse(first_status["customer_live_enabled"])

            # Corrupt safety state fails closed for entry routes, but does not
            # block a stop command that may be needed during an incident.
            (runtime / "trading_kill_switch.json").write_text("not-json", encoding="utf-8")
            locked_add = first_client.post(
                "/api/bot/pairs",
                json={"symbol": "ETHUSDT", "exchange": "binance"},
                headers={"X-CSRF-Token": "first-csrf"},
            )
            self.assertEqual(locked_add.status_code, 423)
            allowed_stop = first_client.post(
                "/api/bot/stop",
                json={"symbol": "BTCUSDT"},
                headers={"X-CSRF-Token": "first-csrf"},
            )
            self.assertEqual(allowed_stop.status_code, 200, allowed_stop.get_json())
            (runtime / "trading_kill_switch.json").write_text(
                '{"engaged":false,"reason":"test reset"}',
                encoding="utf-8",
            )

            second_client = app.test_client()
            with second_client.session_transaction() as session:
                session["user_id"] = int(second_user["id"])
                session["csrf_token"] = "second-csrf"
            second_pairs_before = second_client.get("/api/bot/pairs")
            self.assertEqual(second_pairs_before.status_code, 200)
            self.assertEqual(second_pairs_before.get_json()["pairs"], [])
            second_add = second_client.post(
                "/api/bot/pairs",
                json={"symbol": "ETHUSDT", "exchange": "binance"},
                headers={"X-CSRF-Token": "second-csrf"},
            )
            self.assertEqual(second_add.status_code, 200, second_add.get_json())

            first_pairs_after = first_client.get("/api/bot/pairs").get_json()["pairs"]
            second_pairs_after = second_client.get("/api/bot/pairs").get_json()["pairs"]
            self.assertEqual([item["symbol"] for item in first_pairs_after], ["BTCUSDT"])
            self.assertEqual([item["symbol"] for item in second_pairs_after], ["ETHUSDT"])

            summary = first_client.get("/api/account/summary")
            self.assertEqual(summary.status_code, 200)
            self.assertTrue(summary.get_json()["simulated"])
            self.assertEqual(first_client.get("/api/codex/activity").status_code, 403)


if __name__ == "__main__":
    unittest.main()
