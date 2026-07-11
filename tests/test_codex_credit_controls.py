from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.volatility_bot import VolatilityTwapBot


def load_codex_daemon():
    module_path = ROOT_DIR / "tools" / "codex_daemon.py"
    spec = importlib.util.spec_from_file_location("codex_daemon_for_tests", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load Codex daemon module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CodexCreditControlTests(unittest.TestCase):
    def test_global_budget_allows_only_one_call_per_rolling_hour(self) -> None:
        daemon = load_codex_daemon()
        with tempfile.TemporaryDirectory() as temp_dir:
            state_file = Path(temp_dir) / "budget.json"
            with patch.dict(os.environ, {"CODEX_CLI_MAX_CALLS_PER_HOUR": "1"}):
                first, first_retry = daemon._claim_codex_budget(now=1_000, state_file=state_file)
                second, second_retry = daemon._claim_codex_budget(now=1_001, state_file=state_file)
                next_hour, next_retry = daemon._claim_codex_budget(now=4_601, state_file=state_file)

        self.assertTrue(first)
        self.assertEqual(first_retry, 0)
        self.assertFalse(second)
        self.assertGreaterEqual(second_retry, 3_599)
        self.assertTrue(next_hour)
        self.assertEqual(next_retry, 0)

    def test_inactive_pair_is_not_eligible_for_codex(self) -> None:
        bot = object.__new__(VolatilityTwapBot)
        bot.codex_active_only = True
        bot.dust_position_cost_usdt = Decimal("5")
        bot.state = SimpleNamespace(
            status="waiting",
            position_qty=Decimal("0"),
            position_cost_basis=Decimal("0"),
            active_twap_algo_id=None,
            target_sell_order_id=None,
            stop_loss_order_id=None,
        )

        self.assertFalse(bot._codex_advisory_eligible())
        bot.state.status = "trading"
        self.assertTrue(bot._codex_advisory_eligible())

    def test_invested_request_is_prioritized(self) -> None:
        daemon = load_codex_daemon()
        with tempfile.TemporaryDirectory() as temp_dir:
            flat = Path(temp_dir) / "flat.json"
            invested = Path(temp_dir) / "invested.json"
            flat.write_text('{"priority":1,"requested_at":200}', encoding="utf-8")
            invested.write_text('{"priority":2,"requested_at":100}', encoding="utf-8")

            ordered = sorted([flat, invested], key=daemon._request_sort_key)

        self.assertEqual(ordered[0].stem, "invested")


if __name__ == "__main__":
    unittest.main()
