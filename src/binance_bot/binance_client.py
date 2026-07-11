from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

import requests
from binance.error import ClientError
from binance.spot import Spot


def D(value: object) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class SymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    min_qty: Decimal
    step_size: Decimal
    min_notional: Decimal
    quote_order_qty_market_allowed: bool


class BinanceClient:
    def __init__(self, api_key: str, api_secret: str, dry_run: bool) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self._client = Spot(api_key=api_key, api_secret=api_secret)
        self._session = requests.Session()
        self._base_url = "https://api.binance.com"
        self.dry_run = dry_run
        self._symbols: dict[str, SymbolInfo] | None = None
        self._timestamp_offset_ms = 0

    def get_account(self) -> dict:
        try:
            return self._client.account()
        except ClientError as exc:
            self._raise_client_error(exc)

    def get_balances(self) -> list[dict]:
        return list(self.get_account().get("balances", []))

    def get_api_key_permissions(self) -> dict:
        return self._signed_sapi_request("GET", "/sapi/v1/account/apiRestrictions", {})

    def get_symbols(self) -> dict[str, SymbolInfo]:
        if self._symbols is not None:
            return self._symbols

        exchange_info = self._client.exchange_info()
        symbols: dict[str, SymbolInfo] = {}
        for item in exchange_info.get("symbols", []):
            filters = {entry["filterType"]: entry for entry in item.get("filters", [])}
            lot_size = filters.get("LOT_SIZE", {})
            min_notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
            symbols[item["symbol"]] = SymbolInfo(
                symbol=item["symbol"],
                base_asset=item["baseAsset"],
                quote_asset=item["quoteAsset"],
                min_qty=D(lot_size.get("minQty", "0.00000001")),
                step_size=D(lot_size.get("stepSize", "0.00000001")),
                min_notional=D(min_notional_filter.get("minNotional", "0")),
                quote_order_qty_market_allowed=bool(item.get("quoteOrderQtyMarketAllowed", False)),
            )
        self._symbols = symbols
        return symbols

    def get_symbol(self, symbol: str) -> SymbolInfo:
        return self.get_symbols()[symbol]

    def find_symbol_for_asset(self, asset: str, stable_quote_assets: tuple[str, ...]) -> str | None:
        asset = asset.upper()
        stable_quotes = set(stable_quote_assets)
        candidates = [
            info.symbol
            for info in self.get_symbols().values()
            if info.base_asset == asset and info.quote_asset in stable_quotes
        ]
        if not candidates:
            return None

        preferred = sorted(
            candidates,
            key=lambda value: stable_quote_assets.index(self.get_symbol(value).quote_asset),
        )
        return preferred[0]

    def get_all_prices(self) -> dict[str, Decimal]:
        tickers = self._client.ticker_price()
        if isinstance(tickers, dict):
            return {tickers["symbol"]: D(tickers["price"])}
        return {item["symbol"]: D(item["price"]) for item in tickers}

    def get_price(self, symbol: str) -> Decimal:
        ticker = self._client.ticker_price(symbol=symbol)
        return D(ticker["price"])

    def get_my_trades(self, symbol: str) -> list[dict]:
        trades: list[dict] = []
        from_id: int | None = None
        while True:
            params = {"symbol": symbol, "limit": 1000}
            if from_id is not None:
                params["fromId"] = from_id
            try:
                batch = list(self._client.my_trades(**params))
            except ClientError as exc:
                self._raise_client_error(exc)
            if not batch:
                break
            trades.extend(batch)
            if len(batch) < 1000:
                break
            from_id = int(batch[-1]["id"]) + 1
        trades.sort(key=lambda item: int(item["time"]))
        return trades

    def round_quantity(self, symbol: str, quantity: Decimal) -> Decimal:
        info = self.get_symbol(symbol)
        if info.step_size <= 0:
            return quantity
        steps = (quantity / info.step_size).to_integral_value(rounding=ROUND_DOWN)
        return steps * info.step_size

    def validate_quantity(self, symbol: str, quantity: Decimal, price: Decimal | None = None) -> Decimal:
        info = self.get_symbol(symbol)
        rounded = self.round_quantity(symbol, quantity)
        if rounded < info.min_qty:
            raise ValueError(f"{symbol} quantity {rounded} is below minQty {info.min_qty}")
        if price is not None and info.min_notional > 0 and rounded * price < info.min_notional:
            raise ValueError(f"{symbol} notional {rounded * price} is below minNotional {info.min_notional}")
        return rounded

    def market_sell(self, symbol: str, quantity: Decimal) -> dict:
        payload = {"symbol": symbol, "quantity": format(quantity, "f"), "type": "MARKET", "side": "SELL", "newOrderRespType": "FULL"}
        if self.dry_run:
            payload["status"] = "DRY_RUN"
            return payload
        try:
            return self._client.new_order(**payload)
        except ClientError as exc:
            self._raise_client_error(exc)

    def market_buy_quote(self, symbol: str, quote_order_qty: Decimal) -> dict:
        info = self.get_symbol(symbol)
        payload = {"symbol": symbol, "type": "MARKET", "side": "BUY", "newOrderRespType": "FULL"}
        if info.quote_order_qty_market_allowed:
            payload["quoteOrderQty"] = format(quote_order_qty, "f")
        else:
            raise ValueError(f"{symbol} does not allow quoteOrderQty market buys")
        if self.dry_run:
            payload["status"] = "DRY_RUN"
            return payload
        try:
            return self._client.new_order(**payload)
        except ClientError as exc:
            self._raise_client_error(exc)

    def new_spot_algo_twap(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        duration_seconds: int,
        client_algo_id: str,
        limit_price: Decimal | None = None,
    ) -> dict:
        payload: dict[str, object] = {
            "symbol": symbol,
            "side": side,
            "quantity": format(quantity, "f"),
            "duration": int(duration_seconds),
            "clientAlgoId": client_algo_id,
        }
        if limit_price is not None:
            payload["limitPrice"] = format(limit_price, "f")
        if self.dry_run:
            return {
                "clientAlgoId": client_algo_id,
                "success": True,
                "code": 0,
                "msg": "DRY_RUN",
                "status": "DRY_RUN",
            }
        return self._signed_sapi_request("POST", "/sapi/v1/algo/spot/newOrderTwap", payload)

    def cancel_spot_algo_order(self, algo_id: int) -> dict:
        if self.dry_run:
            return {"algoId": algo_id, "success": True, "code": 0, "msg": "DRY_RUN", "status": "DRY_RUN"}
        return self._signed_sapi_request("DELETE", "/sapi/v1/algo/spot/order", {"algoId": algo_id})

    def get_spot_algo_open_orders(self) -> dict:
        if self.dry_run:
            return {"total": 0, "orders": []}
        return self._signed_sapi_request("GET", "/sapi/v1/algo/spot/openOrders", {})

    def get_spot_algo_historical_orders(self, symbol: str | None = None, side: str | None = None) -> dict:
        if self.dry_run:
            return {"total": 0, "orders": []}
        payload: dict[str, object] = {"page": 1, "pageSize": 100}
        if symbol:
            payload["symbol"] = symbol
        if side:
            payload["side"] = side
        return self._signed_sapi_request("GET", "/sapi/v1/algo/spot/historicalOrders", payload)

    def get_spot_algo_sub_orders(self, algo_id: int) -> dict:
        if self.dry_run:
            return {"total": 0, "executedQty": "0", "executedAmt": "0", "subOrders": []}
        return self._signed_sapi_request("GET", "/sapi/v1/algo/spot/subOrders", {"algoId": algo_id, "page": 1, "pageSize": 100})

    def find_spot_algo_order(
        self,
        symbol: str,
        client_algo_id: str | None = None,
        algo_id: int | None = None,
    ) -> dict | None:
        open_orders = self.get_spot_algo_open_orders().get("orders", [])
        for order in open_orders:
            if algo_id is not None and int(order.get("algoId", -1)) == algo_id:
                return order
            if client_algo_id and order.get("clientAlgoId") == client_algo_id:
                return order

        historical_orders = self.get_spot_algo_historical_orders(symbol=symbol).get("orders", [])
        for order in historical_orders:
            if algo_id is not None and int(order.get("algoId", -1)) == algo_id:
                return order
            if client_algo_id and order.get("clientAlgoId") == client_algo_id:
                return order
        return None

    def _signed_sapi_request(self, method: str, path: str, params: dict[str, object]) -> dict:
        normalized = self._normalize_params(params)
        normalized.setdefault("recvWindow", "5000")
        response = self._send_signed_sapi_request(method, path, normalized)
        if self._is_timestamp_error(response):
            self._sync_timestamp_offset()
            response = self._send_signed_sapi_request(method, path, normalized)
        return self._handle_response(response)

    def _send_signed_sapi_request(self, method: str, path: str, normalized: dict[str, str]) -> requests.Response:
        signed_params = dict(normalized)
        signed_params["timestamp"] = str(int(time.time() * 1000) + self._timestamp_offset_ms)
        payload = urllib.parse.urlencode(signed_params, quote_via=urllib.parse.quote, safe="")
        signature = hmac.new(self.api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{self._base_url}{path}?{payload}&signature={signature}"
        return self._session.request(method=method, url=url, headers={"X-MBX-APIKEY": self.api_key}, timeout=30)

    def _sync_timestamp_offset(self) -> None:
        response = self._session.get(f"{self._base_url}/api/v3/time", timeout=10)
        response.raise_for_status()
        payload = response.json()
        server_time = int(payload["serverTime"])
        self._timestamp_offset_ms = server_time - int(time.time() * 1000)

    @staticmethod
    def _is_timestamp_error(response: requests.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        try:
            return int(payload.get("code", 0)) == -1021
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _normalize_params(params: dict[str, object]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for key, value in params.items():
            if value is None:
                continue
            if isinstance(value, Decimal):
                normalized[key] = format(value, "f")
                continue
            normalized[key] = str(value)
        return normalized

    def _handle_response(self, response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            code = payload.get("code")
            success = payload.get("success")
            if code not in (None, 0, "0"):
                self._raise_spot_algo_error(response.status_code, int(code), payload.get("msg"), payload)
            if success is False:
                self._raise_spot_algo_error(response.status_code, None, payload.get("msg"), payload)

        if response.ok and isinstance(payload, dict):
            return payload
        if response.ok and isinstance(payload, list):
            return {"items": payload}

        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg") if isinstance(payload, dict) else response.text
        self._raise_spot_algo_error(response.status_code, code, message, payload)

    @staticmethod
    def _raise_spot_algo_error(
        status_code: int,
        code: int | str | None,
        message: str | None,
        payload: dict | None,
    ) -> None:
        normalized_code = int(code) if isinstance(code, str) and code.lstrip("-").isdigit() else code
        if code == -1002:
            raise RuntimeError(
                "Binance Spot Algo Orders API access is not authorized for this API key or account. "
                "The key can still work for normal spot account endpoints while Binance rejects the native Spot Algo endpoints."
            )
        if status_code == 401 or normalized_code == -2015:
            raise RuntimeError(
                "Binance API authentication failed. Check BINANCE_API_KEY, BINANCE_API_SECRET, "
                "spot permissions, and any IP whitelist on the Binance key."
            )
        if normalized_code == -20204:
            raise RuntimeError(
                "Binance rejected the Spot Algo TWAP notional for this symbol. "
                "The configured quote budget is outside Binance's allowed USD notional range for this algo order."
            )
        if status_code == 429:
            raise RuntimeError("Binance API rate limit hit. Slow down the polling or wait before retrying.")
        raise RuntimeError(f"Binance API request failed: {normalized_code} {message or payload}")

    @staticmethod
    def _raise_client_error(exc: ClientError) -> None:
        status_code = getattr(exc, "status_code", None)
        error_code = getattr(exc, "error_code", None)
        error_message = getattr(exc, "error_message", str(exc))
        if status_code == 401 or error_code == -2015:
            raise RuntimeError(
                "Binance API authentication failed. Check BINANCE_API_KEY, BINANCE_API_SECRET, "
                "spot permissions, and any IP whitelist on the Binance key."
            ) from exc
        raise RuntimeError(f"Binance API request failed: {error_code} {error_message}") from exc
