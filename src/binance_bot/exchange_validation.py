from __future__ import annotations

from typing import Any

from .binance_client import BinanceClient
from .bitget_client import BitgetClient


def validate_exchange_credentials(
    exchange: str,
    api_key: str,
    api_secret: str,
    passphrase: str = "",
) -> dict[str, Any]:
    """Validate credentials with read-only exchange calls and fail closed.

    This function never places, cancels, transfers, or withdraws anything.
    """

    normalized = exchange.strip().lower()
    if normalized == "binance":
        return _validate_binance(api_key, api_secret)
    if normalized == "bitget":
        return _validate_bitget(api_key, api_secret, passphrase)
    raise ValueError("Unsupported exchange")


def _validate_binance(api_key: str, api_secret: str) -> dict[str, Any]:
    client = BinanceClient(api_key.strip(), api_secret.strip(), dry_run=True)
    permissions = client.get_api_key_permissions()
    errors: list[str] = []
    warnings: list[str] = []

    if not permissions.get("enableReading"):
        errors.append("Read permission is required")
    if not permissions.get("enableSpotAndMarginTrading"):
        errors.append("Spot trading permission is required")
    if permissions.get("enableWithdrawals"):
        errors.append("Withdrawal permission must be disabled")
    if permissions.get("enableInternalTransfer"):
        errors.append("Internal transfer permission must be disabled")
    if permissions.get("permitsUniversalTransfer"):
        errors.append("Universal transfer permission must be disabled")
    if permissions.get("enableFutures") or permissions.get("enableMargin") or permissions.get("enablePortfolioMarginTrading"):
        warnings.append("Extra margin/futures permissions are enabled but not required for spot automation")
    if not permissions.get("ipRestrict"):
        errors.append("IP restriction must be enabled for a production connection")

    return {
        "exchange": "binance",
        "safe": not errors,
        "status": "verified_safe" if not errors else "rejected_unsafe_permissions",
        "errors": errors,
        "warnings": warnings,
        "read_enabled": bool(permissions.get("enableReading")),
        "spot_trade_enabled": bool(permissions.get("enableSpotAndMarginTrading")),
        "ip_restricted": bool(permissions.get("ipRestrict")),
        "withdrawal_enabled": bool(permissions.get("enableWithdrawals")),
        "transfer_enabled": bool(permissions.get("enableInternalTransfer") or permissions.get("permitsUniversalTransfer")),
    }


def _validate_bitget(api_key: str, api_secret: str, passphrase: str) -> dict[str, Any]:
    if not passphrase.strip():
        raise ValueError("Bitget passphrase is required")
    client = BitgetClient(api_key.strip(), api_secret.strip(), passphrase.strip(), dry_run=False)
    account_mode, info = client.get_account_info()
    errors: list[str] = []
    warnings: list[str] = []
    ips = str(info.get("ips") or "").strip()

    if account_mode == "uta":
        permissions = {str(item).lower() for item in info.get("permissions") or []}
        trade_enabled = "uta_trade" in permissions
        withdrawal_enabled = "withdraw" in permissions
        transfer_enabled = any("transfer" in permission for permission in permissions)
    else:
        permissions = {str(item).lower() for item in info.get("authorities") or []}
        trade_enabled = "stow" in permissions
        withdrawal_enabled = "wwow" in permissions
        transfer_enabled = "wtow" in permissions

    if not trade_enabled:
        errors.append("Spot trade permission is required")
    if withdrawal_enabled:
        errors.append("Withdrawal permission must be disabled")
    if transfer_enabled:
        errors.append("Wallet transfer permission must be disabled")
    if not ips:
        errors.append("IP restriction must be enabled for a production connection")
    if account_mode == "classic":
        warnings.append("Classic account cannot automate Bitget Reality stock orders; UTA plus UID whitelisting is required")

    return {
        "exchange": "bitget",
        "safe": not errors,
        "status": "verified_safe" if not errors else "rejected_unsafe_permissions",
        "errors": errors,
        "warnings": warnings,
        "account_mode": account_mode,
        "trade_enabled": trade_enabled,
        "ip_restricted": bool(ips),
        "withdrawal_enabled": withdrawal_enabled,
        "transfer_enabled": transfer_enabled,
        "permissions": sorted(permissions),
    }
