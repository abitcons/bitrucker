from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.market_readiness import build_market_readiness
from binance_bot.exchange_validation import validate_exchange_credentials
from binance_bot.saas import LEGAL_VERSION, SaaSStore


class MarketReadinessTests(unittest.TestCase):
    def test_signup_acceptance_is_versioned(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SaaSStore(Path(temp_dir))
            user = store.create_user(
                "user@example.com",
                "a-strong-password",
                accepted_terms_version=LEGAL_VERSION,
            )

            self.assertEqual(user["accepted_terms_version"], LEGAL_VERSION)
            self.assertTrue(user["accepted_terms_at"])
            self.assertTrue(user["risk_acknowledged_at"])

    def test_failed_logins_are_rate_limited_durably(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SaaSStore(Path(temp_dir))
            for _ in range(5):
                store.record_login_attempt("user@example.com", "203.0.113.10", False)

            allowed, remaining = store.login_allowed("user@example.com", "203.0.113.10")

            self.assertFalse(allowed)
            self.assertEqual(remaining, 0)

    def test_pending_invoice_is_idempotent_and_payment_reference_is_unique(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"SAAS_USDT_PAYMENT_ADDRESS": "merchant-wallet", "SAAS_USDT_PAYMENT_NETWORK": "TRC20"},
        ):
            store = SaaSStore(Path(temp_dir))
            first_user = store.create_user("first@example.com", "a-strong-password")
            second_user = store.create_user("second@example.com", "a-strong-password")

            first_invoice = store.create_invoice(int(first_user["id"]))
            repeated_invoice = store.create_invoice(int(first_user["id"]))
            self.assertEqual(first_invoice["id"], repeated_invoice["id"])

            store.approve_invoice(int(first_invoice["id"]), "verified-transaction-123")
            second_invoice = store.create_invoice(int(second_user["id"]))
            with self.assertRaisesRegex(ValueError, "already assigned"):
                store.approve_invoice(int(second_invoice["id"]), "verified-transaction-123")

    def test_readiness_fails_closed_for_missing_market_controls(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {}, clear=True):
            store = SaaSStore(Path(temp_dir))
            readiness = build_market_readiness(Path(temp_dir), store, {"SESSION_COOKIE_SECURE": False})
            critical_ids = {item["id"] for item in readiness["critical_failures"]}

            self.assertFalse(readiness["ready"])
            self.assertIn("tenant_isolation", critical_ids)
            self.assertIn("secure_cookies", critical_ids)
            self.assertIn("legal_review", critical_ids)

    def test_totp_and_recovery_codes_are_verified_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SaaSStore(Path(temp_dir))
            user = store.create_user("owner@example.com", "a-strong-password", role="owner", is_owner=True)
            setup = store.begin_mfa_setup(int(user["id"]))
            current_code = store._totp_code(setup["secret"])

            recovery_codes = store.enable_mfa(int(user["id"]), current_code)
            verified, method = store.verify_user_mfa(int(user["id"]), store._totp_code(setup["secret"]))
            self.assertTrue(verified)
            self.assertEqual(method, "totp")

            recovered, method = store.verify_user_mfa(int(user["id"]), recovery_codes[0])
            self.assertTrue(recovered)
            self.assertEqual(method, "recovery_code")
            reused, _method = store.verify_user_mfa(int(user["id"]), recovery_codes[0])
            self.assertFalse(reused)

    def test_exchange_vault_stores_validated_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = SaaSStore(Path(temp_dir))
            user = store.create_user("user@example.com", "a-strong-password")
            store.store_exchange_credentials(
                int(user["id"]),
                "bitget",
                "api-key-1234567890",
                "secret-value",
                passphrase="passphrase-value",
                validation_status="verified_safe",
                validation={"safe": True, "ip_restricted": True},
            )

            connection = store.get_exchange_credentials(int(user["id"]), "bitget")
            summary = store.list_exchange_connections(int(user["id"]))[0]
            self.assertEqual(connection["api_secret"], "secret-value")
            self.assertEqual(connection["passphrase"], "passphrase-value")
            self.assertEqual(summary["validation_status"], "verified_safe")
            self.assertNotIn("secret", str(summary).lower())

    @patch("binance_bot.exchange_validation.BinanceClient")
    def test_binance_validation_rejects_withdrawal_permission(self, client_class) -> None:
        client_class.return_value.get_api_key_permissions.return_value = {
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": True,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "ipRestrict": True,
        }

        result = validate_exchange_credentials("binance", "key", "secret")

        self.assertFalse(result["safe"])
        self.assertIn("Withdrawal permission must be disabled", result["errors"])

    @patch("binance_bot.exchange_validation.BitgetClient")
    def test_bitget_validation_accepts_trade_only_ip_restricted_key(self, client_class) -> None:
        client_class.return_value.get_account_info.return_value = (
            "classic",
            {"ips": "203.0.113.4", "authorities": ["stow"]},
        )

        result = validate_exchange_credentials("bitget", "key", "secret", "passphrase")

        self.assertTrue(result["safe"])
        self.assertEqual(result["account_mode"], "classic")
        self.assertTrue(result["ip_restricted"])


if __name__ == "__main__":
    unittest.main()
