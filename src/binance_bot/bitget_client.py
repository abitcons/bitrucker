from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from decimal import Decimal, ROUND_DOWN

import requests


LOGGER = logging.getLogger(__name__)


class BitgetClient:
    """Thin wrapper around the Bitget Spot v2 REST API."""

    BASE_URL = "https://api.bitget.com"

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        passphrase: str,
        dry_run: bool,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.dry_run = dry_run
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # signing
    # ------------------------------------------------------------------
    def _sign(self, timestamp: str, method: str, path: str, body: str) -> str:
        prehash = timestamp + method.upper() + path + body
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode("utf-8")

    def _headers(self, method: str, path: str, body: str) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(timestamp, method, path, body),
            "ACCESS-TIMESTAMP": timestamp,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }

    # ------------------------------------------------------------------
    # generic request
    # ------------------------------------------------------------------
    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        body = json.dumps(payload) if payload else ""
        headers = self._headers(method, path, body)
        url = f"{self.BASE_URL}{path}"
        response = self._session.request(
            method=method,
            url=url,
            headers=headers,
            data=body if body else None,
            timeout=30,
        )
        try:
            data = response.json()
        except ValueError:
            data = None

        if not response.ok or (isinstance(data, dict) and str(data.get("code", "00000")) != "00000"):
            msg = data.get("msg", response.text) if isinstance(data, dict) else response.text
            code = data.get("code") if isinstance(data, dict) else response.status_code
            raise RuntimeError(f"Bitget API error {code}: {msg}")

        return data

    # ------------------------------------------------------------------
    # account
    # ------------------------------------------------------------------
    def get_account_assets(self) -> list[dict]:
        if self.dry_run:
            return []
        data = self._request("GET", "/api/v2/spot/account/assets")
        return data.get("data", [])

    def get_account_info(self) -> tuple[str, dict]:
        """Return account mode and API-key permissions using read-only calls."""
        try:
            data = self._request("GET", "/api/v3/account/info")
            return "uta", data.get("data", {})
        except RuntimeError as exc:
            if "40084" not in str(exc) and "classic" not in str(exc).lower():
                raise
        data = self._request("GET", "/api/v2/spot/account/info")
        return "classic", data.get("data", {})

    # ------------------------------------------------------------------
    # orders
    # ------------------------------------------------------------------
    def market_buy(self, symbol: str, quantity: Decimal) -> dict:
        payload = {
            "symbol": symbol,
            "side": "buy",
            "orderType": "market",
            "size": format(quantity, "f"),
        }
        if self.dry_run:
            return {"orderId": "DRY_RUN", "clientOid": "", "status": "DRY_RUN", **payload}
        return self._request("POST", "/api/v2/spot/trade/place-order", payload)

    def market_sell(self, symbol: str, quantity: Decimal) -> dict:
        payload = {
            "symbol": symbol,
            "side": "sell",
            "orderType": "market",
            "size": format(quantity, "f"),
        }
        if self.dry_run:
            return {"orderId": "DRY_RUN", "clientOid": "", "status": "DRY_RUN", **payload}
        return self._request("POST", "/api/v2/spot/trade/place-order", payload)

    def get_order_info(self, symbol: str, order_id: str) -> dict | None:
        if self.dry_run:
            return None
        try:
            data = self._request(
                "GET",
                f"/api/v2/spot/trade/orderInfo?symbol={symbol}&orderId={order_id}",
            )
            orders = data.get("data", [])
            return orders[0] if orders else None
        except RuntimeError:
            return None

    def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        if self.dry_run:
            return []
        path = "/api/v2/spot/trade/unfilled-orders"
        if symbol:
            path += f"?symbol={symbol}"
        data = self._request("GET", path)
        return data.get("data", [])

    def get_history_orders(self, symbol: str | None = None) -> list[dict]:
        if self.dry_run:
            return []
        path = "/api/v2/spot/trade/history-orders"
        if symbol:
            path += f"?symbol={symbol}"
        data = self._request("GET", path)
        return data.get("data", [])
