from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.volatility_bot import TwapSwingBot


class BitgetPrimaryExchangeTests(unittest.TestCase):
    def make_bot(self, temp_dir: str) -> TwapSwingBot:
        env = {
            "PRIMARY_EXCHANGE": "bitget",
            "BITGET_AccessAPIKey": "test-key",
            "BITGET_secretkey": "test-secret",
            "BITGET_PASSWORD": "test-passphrase",
        }
        with patch.dict(os.environ, env):
            return TwapSwingBot(Path(temp_dir), "unused", "unused", dry_run=True)

    def test_bitget_is_loaded_as_primary_from_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            self.assertEqual(bot.primary_exchange, "bitget")
            self.assertTrue(bot.bitget_api_key)

    def test_bitget_market_buy_uses_quote_amount(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            captured: dict = {}
            bot.is_stock_instrument = lambda symbol=None: False  # type: ignore[method-assign]

            def fake_request(method: str, path: str, params: dict) -> dict:
                captured.update({"method": method, "path": path, "params": params})
                return {"code": "00000", "data": {"orderId": "42", "clientOid": "cid"}}

            bot._bitget_private_request = fake_request  # type: ignore[method-assign]
            result = bot._send_bitget_signed_request(
                "POST",
                "/api/v3/order",
                {"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quoteOrderQty": "25.00"},
            )

            self.assertEqual(captured["path"], "/api/v2/spot/trade/place-order")
            self.assertEqual(captured["params"]["size"], "25.00")
            self.assertNotIn("force", captured["params"])
            self.assertEqual(result["orderId"], "42")

    def test_supported_symbols_come_from_selected_exchange_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot._get_bitget_public_json = lambda path, timeout: {  # type: ignore[method-assign]
                "code": "00000",
                "data": [
                    {"symbol": "BNBUSDT", "status": "online", "quoteCoin": "USDT"},
                    {"symbol": "OFFLINEUSDT", "status": "offline", "quoteCoin": "USDT"},
                ],
            }

            supported = bot.get_exchange_supported_symbols(force=True)

            self.assertIn("BNBUSDT", supported)
            self.assertNotIn("BNBFDUSD", supported)
            self.assertNotIn("OFFLINEUSDT", supported)

    def test_unsupported_quote_maps_to_supported_pair_for_same_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot.get_exchange_supported_symbols = lambda force=False: {"BNBUSDT", "BTCUSDT"}  # type: ignore[method-assign]

            message = bot.switch_symbol("BNBFDUSD")

            self.assertEqual(bot.symbol, "BNBUSDT")
            self.assertEqual(bot.state.symbol, "BNBUSDT")
            self.assertIn("Mapped unsupported BNBFDUSD to BNBUSDT", message)

    def test_bitget_order_and_fill_fields_are_normalized_for_position_tracking(self) -> None:
        order = TwapSwingBot._bitget_order_to_binance({
            "orderId": "7",
            "symbol": "RINTCUSDT",
            "side": "sell",
            "orderType": "limit",
            "force": "gtc",
            "status": "live",
            "size": "0.2133",
        })
        fill = TwapSwingBot._bitget_fill_to_binance({
            "tradeId": "8",
            "symbol": "BNBUSDT",
            "side": "buy",
            "priceAvg": "577.8",
            "size": "0.0206",
            "amount": "11.90268",
            "feeDetail": '{"BNB":{"feeCoinCode":"BNB","totalFee":"-0.0000206"}}',
            "tradeScope": "taker",
        })

        self.assertEqual(order["type"], "LIMIT")
        self.assertEqual(order["timeInForce"], "GTC")
        self.assertEqual(fill["price"], "577.8")
        self.assertEqual(fill["qty"], "0.0206")
        self.assertEqual(fill["quoteQty"], "11.90268")
        self.assertEqual(fill["commission"], "0.0000206")
        self.assertEqual(fill["commissionAsset"], "BNB")

    def test_exchange_switch_rejects_pair_not_listed_on_target_exchange(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot.symbol = "BNBFDUSD"
            bot.state.symbol = "BNBFDUSD"
            bot.get_exchange_supported_symbols = lambda force=False: {"BNBUSDT"}  # type: ignore[method-assign]

            with self.assertRaisesRegex(ValueError, "Use BNBUSDT before switching"):
                bot.set_primary_exchange("binance")

            self.assertEqual(bot.primary_exchange, "bitget")
            self.assertEqual(bot.state.primary_exchange, "bitget")

    def test_exchange_switch_is_blocked_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot.running = True
            with self.assertRaisesRegex(RuntimeError, "Stop the bot"):
                bot.set_primary_exchange("binance")

    def test_reality_stock_is_classified_and_plain_ticker_maps_to_exchange_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot._get_bitget_public_json = lambda path, timeout: {  # type: ignore[method-assign]
                "code": "00000",
                "data": [{
                    "symbol": "rNVDAUSDT",
                    "status": "online",
                    "baseCoin": "rNVDA",
                    "quoteCoin": "USDT",
                    "symbolType": "stock",
                    "isReality": "yes",
                }],
            }

            bot.get_exchange_instruments(force=True)
            message = bot.switch_symbol("NVDA")

            self.assertEqual(bot.symbol, "RNVDAUSDT")
            self.assertTrue(bot.is_stock_instrument())
            self.assertEqual(bot.get_instrument_type(), "stock")
            self.assertEqual(bot._stock_underlying_ticker(), "NVDA")
            self.assertIn("RNVDAUSDT", message)

    def test_stock_liquidation_signal_does_not_query_crypto_futures(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot._exchange_instruments_cache = (time.time(), {
                "RINTCUSDT": {"symbol": "RINTCUSDT", "baseCoin": "rINTC", "instrument_type": "stock", "is_reality": True},
            })
            futures_request = Mock(side_effect=AssertionError("crypto futures should not be queried for stocks"))
            bot._get_binance_futures_json = futures_request  # type: ignore[method-assign]

            signal = bot._get_liquidation_signal("RINTCUSDT")

            self.assertEqual(signal["bias"], "not_applicable")
            futures_request.assert_not_called()

    def test_classic_account_blocks_reality_order_before_any_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot._exchange_instruments_cache = (time.time(), {
                "RINTCUSDT": {"symbol": "RINTCUSDT", "api_symbol": "rINTCUSDT", "baseCoin": "rINTC", "instrument_type": "stock", "is_reality": True},
            })
            bot._get_bitget_account_mode = lambda force=False: ("classic", "Classic account")  # type: ignore[method-assign]
            private_request = Mock(side_effect=AssertionError("no order write should be sent"))
            bot._bitget_private_request = private_request  # type: ignore[method-assign]

            with self.assertRaisesRegex(RuntimeError, "Unified Trading Account"):
                bot._send_bitget_signed_request("POST", "/api/v3/order", {
                    "symbol": "RINTCUSDT",
                    "side": "SELL",
                    "type": "LIMIT",
                    "quantity": "0.2",
                    "price": "125",
                })

            private_request.assert_not_called()

    def test_uta_reality_order_uses_dedicated_endpoint_and_qty_field(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot._exchange_instruments_cache = (time.time(), {
                "RNVDAUSDT": {"symbol": "RNVDAUSDT", "api_symbol": "rNVDAUSDT", "baseCoin": "rNVDA", "instrument_type": "stock", "is_reality": True},
            })
            bot._get_bitget_account_mode = lambda force=False: ("uta", "UTA")  # type: ignore[method-assign]
            captured: dict = {}

            def fake_request(method: str, path: str, params: dict) -> dict:
                captured.update({"method": method, "path": path, "params": params})
                return {"code": "00000", "data": {"orderId": "stock-42", "clientOid": "stock-cid"}}

            bot._bitget_private_request = fake_request  # type: ignore[method-assign]
            result = bot._send_bitget_signed_request("POST", "/api/v3/order", {
                "symbol": "RNVDAUSDT",
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": "25.00",
            })

            self.assertEqual(captured["path"], "/api/v3/trade/place-reality-order")
            self.assertEqual(captured["params"]["symbol"], "rNVDAUSDT")
            self.assertEqual(captured["params"]["qty"], "25.00")
            self.assertNotIn("size", captured["params"])
            self.assertEqual(result["orderId"], "stock-42")

    def test_classic_stock_target_order_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot.dry_run = False
            bot.symbol = "RINTCUSDT"
            bot.state.symbol = bot.symbol
            bot._exchange_instruments_cache = (time.time(), {
                bot.symbol: {"symbol": bot.symbol, "baseCoin": "rINTC", "instrument_type": "stock", "is_reality": True},
            })
            bot.state.position_qty = Decimal("0.2133")
            bot.state.target_sell_order_id = "existing-stock-order"
            bot.state.target_sell_status = "NEW"
            bot._reconcile_open_target_sell_orders = Mock()  # type: ignore[method-assign]
            bot._stock_trading_block_reason = lambda symbol=None: "Classic account cannot write Reality orders"  # type: ignore[method-assign]
            ratchet = Mock(side_effect=AssertionError("target must not be changed"))
            bot._ratchet_target_profit_if_needed = ratchet  # type: ignore[method-assign]

            bot._ensure_target_sell_order(force=True)

            self.assertEqual(bot.state.target_sell_order_id, "existing-stock-order")
            self.assertEqual(bot.state.target_sell_status, "NEW")
            ratchet.assert_not_called()

    def test_stock_stop_loss_is_software_monitored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            bot = self.make_bot(temp_dir)
            bot.symbol = "RINTCUSDT"
            bot.state.symbol = bot.symbol
            bot._exchange_instruments_cache = (time.time(), {
                bot.symbol: {"symbol": bot.symbol, "baseCoin": "rINTC", "instrument_type": "stock", "is_reality": True},
            })
            bot._quantity_step_size_cache = Decimal("0.0001")
            bot._price_tick_size_cache = Decimal("0.01")
            bot.state.position_qty = Decimal("1")
            bot.state.position_cost_basis = Decimal("100")
            bot.state.current_price = Decimal("101")
            order_request = Mock(side_effect=AssertionError("no exchange stop order should be sent"))
            bot._send_signed_request = order_request  # type: ignore[method-assign]

            bot._ensure_stop_loss_order(force=True)

            self.assertEqual(bot.state.stop_loss_status, "software_monitoring")
            self.assertGreater(bot.state.stop_loss_price, Decimal("0"))
            order_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
