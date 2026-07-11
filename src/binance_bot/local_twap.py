from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
BINANCE_BASE_URL = "https://api.binance.com"
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_LOWER_BOUND = Decimal("50000")
DEFAULT_UPPER_BOUND = Decimal("60000")
DEFAULT_BUDGET = Decimal("1100")
DEFAULT_DURATION_SECONDS = 8 * 60 * 60
DEFAULT_POLL_SECONDS = 300
DEFAULT_PROFIT_TAKE_PCT = Decimal("0.02")
DEFAULT_ORDER_TTL_SECONDS = 900


@dataclass(frozen=True)
class LocalTwapConfig:
    root_dir: Path
    symbol: str = DEFAULT_SYMBOL
    quote_budget_usdt: Decimal = DEFAULT_BUDGET
    lower_price_usdt: Decimal = DEFAULT_LOWER_BOUND
    upper_price_usdt: Decimal = DEFAULT_UPPER_BOUND
    duration_seconds: int = DEFAULT_DURATION_SECONDS
    poll_interval_seconds: int = DEFAULT_POLL_SECONDS
    profit_take_pct: Decimal = DEFAULT_PROFIT_TAKE_PCT
    dry_run: bool = True
    order_ttl_seconds: int = DEFAULT_ORDER_TTL_SECONDS
    state_file: Path | None = None

    @property
    def runtime_state_file(self) -> Path:
        return self.state_file or (self.root_dir / "runtime" / "local_twap_state.json")


class BinanceRestClient:
    def __init__(self, api_key: str, api_secret: str, dry_run: bool) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.dry_run = dry_run

    def get_price(self, symbol: str) -> Decimal:
        payload = self._public_json("/api/v3/ticker/price", {"symbol": symbol})
        return Decimal(str(payload["price"]))

    def get_klines(self, symbol: str, interval: str = "1m", limit: int = 30) -> list[list[Any]]:
        payload = self._public_json("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        if not isinstance(payload, list):
            return []
        return payload

    def get_exchange_info(self, symbol: str) -> dict[str, Any]:
        payload = self._public_json("/api/v3/exchangeInfo", {"symbol": symbol})
        if not isinstance(payload, dict):
            raise RuntimeError(f"Unexpected exchangeInfo payload for {symbol}: {payload!r}")
        return payload

    def get_account(self) -> dict[str, Any]:
        return self._signed_json("GET", "/api/v3/account", {})

    def get_open_orders(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._signed_json("GET", "/api/v3/openOrders", {"symbol": symbol})
        if isinstance(payload, list):
            return [dict(order) for order in payload if isinstance(order, dict)]
        return []

    def get_my_trades(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._signed_json("GET", "/api/v3/myTrades", {"symbol": symbol, "limit": 1000})
        if isinstance(payload, list):
            return [dict(trade) for trade in payload if isinstance(trade, dict)]
        return []

    def place_limit_buy(self, symbol: str, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        return self._place_limit_order(symbol, "BUY", quantity, price)

    def place_limit_sell(self, symbol: str, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        return self._place_limit_order(symbol, "SELL", quantity, price)

    def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        if self.dry_run:
            return {"symbol": symbol, "orderId": order_id, "status": "CANCELED", "dryRun": True}
        return self._signed_json("DELETE", "/api/v3/order", {"symbol": symbol, "orderId": order_id})

    def _place_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "side": side,
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": format(quantity, "f"),
            "price": format(price, "f"),
            "newOrderRespType": "FULL",
        }
        if self.dry_run:
            payload["status"] = "DRY_RUN"
            payload["orderId"] = int(time.time() * 1000)
            return payload
        return self._signed_json("POST", "/api/v3/order", payload)

    def _public_json(self, path: str, params: dict[str, Any]) -> Any:
        query = urllib.parse.urlencode({key: str(value) for key, value in params.items() if value is not None})
        url = f"{BINANCE_BASE_URL}{path}" + (f"?{query}" if query else "")
        request = urllib.request.Request(url, headers={"User-Agent": "adaptive-twap-bot"})
        return self._read_json(request)

    def _signed_json(self, method: str, path: str, params: dict[str, Any]) -> Any:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("BINANCE_API_KEY and BINANCE_API_SECRET must be set")

        payload = {key: value for key, value in params.items() if value is not None}
        payload["timestamp"] = int(time.time() * 1000)
        payload.setdefault("recvWindow", 5000)
        query = urllib.parse.urlencode({key: str(value) for key, value in payload.items()}, quote_via=urllib.parse.quote, safe="")
        signature = hmac.new(self.api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
        url = f"{BINANCE_BASE_URL}{path}?{query}&signature={signature}"
        request = urllib.request.Request(url, headers={"X-MBX-APIKEY": self.api_key, "User-Agent": "adaptive-twap-bot"}, method=method)
        return self._read_json(request)

    def _read_json(self, request: urllib.request.Request) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Binance request failed: HTTP {exc.code} {raw}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Binance request failed: {exc.reason}") from exc

        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Unable to decode Binance response: {raw[:200]}") from exc


class LocalTwapState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


class AdaptiveTwapRunner:
    def __init__(self, config: LocalTwapConfig, client: BinanceRestClient) -> None:
        self.config = config
        self.client = client
        self.state = LocalTwapState(config.runtime_state_file)

    def run(self) -> None:
        state = self._seed_state()
        started_at = float(state["started_at"])
        end_at = started_at + self.config.duration_seconds
        while time.time() < end_at:
            state = self._step(state, end_at)
            self.state.save(state)
            if state.get("done"):
                break
            time.sleep(self.config.poll_interval_seconds)
        state["done"] = True
        self.state.save(state)

    def _seed_state(self) -> dict[str, Any]:
        return {
            "symbol": self.config.symbol,
            "started_at": time.time(),
            "quote_budget_total": format(self.config.quote_budget_usdt, "f"),
            "quote_budget_remaining": format(self.config.quote_budget_usdt, "f"),
            "filled_base_qty": "0",
            "realized_quote": "0",
            "done": False,
            "open_order": None,
            "last_action": None,
        }

    def _step(self, state: dict[str, Any], end_at: float) -> dict[str, Any]:
        symbol = self.config.symbol
        price = self.client.get_price(symbol)
        candles = self.client.get_klines(symbol, "1m", 30)
        stats = self._market_stats(candles)
        remaining_seconds = max(0.0, end_at - time.time())
        remaining_slices = max(1, math.ceil(remaining_seconds / max(1, self.config.poll_interval_seconds)))
        open_orders = self.client.get_open_orders(symbol)
        open_order = next((order for order in open_orders if str(order.get("symbol")) == symbol), None)

        if open_order is not None:
            stale = self._is_order_stale(open_order, state)
            drift = self._order_price_drift(open_order, price)
            if stale or drift > Decimal("0.004"):
                order_id = int(open_order["orderId"])
                LOGGER.info("Canceling stale order %s on %s (stale=%s drift=%s)", order_id, symbol, stale, drift)
                self.client.cancel_order(symbol, order_id)
                state["open_order"] = None
                state["last_action"] = "cancel"
            else:
                state["open_order"] = self._compact_order(open_order)
                state["last_action"] = "hold"
                return state

        account = self.client.get_account()
        position = self._position_snapshot(account, symbol)
        avg_cost = position["avg_cost"]
        free_base_qty = position["free_base_qty"]
        quote_budget_remaining = Decimal(str(state.get("quote_budget_remaining", "0")))
        lower = self.config.lower_price_usdt
        upper = self.config.upper_price_usdt
        midpoint = (lower + upper) / Decimal("2")
        trend = stats["trend"]
        volatility = stats["volatility"]

        within_band = lower <= price <= upper
        profit_ready = avg_cost is not None and avg_cost > 0 and price >= avg_cost * (Decimal("1") + self.config.profit_take_pct)
        sell_bias = free_base_qty > 0 and (price >= upper or profit_ready)
        buy_bias = within_band and quote_budget_remaining > Decimal("0") and not sell_bias

        if sell_bias:
            action = "SELL"
            side_price = self._build_sell_price(price, avg_cost, trend, volatility, lower, upper)
            qty = self._build_sell_quantity(free_base_qty, remaining_slices)
            if qty is None:
                state["last_action"] = "wait"
                return state
            order = self.client.place_limit_sell(symbol, qty, side_price)
            state["last_action"] = action.lower()
            state["open_order"] = self._compact_order(order)
            LOGGER.info("Placed SELL %s qty=%s price=%s avg_cost=%s price_now=%s", symbol, qty, side_price, avg_cost, price)
            return state

        if buy_bias:
            action = "BUY"
            side_price = self._build_buy_price(price, trend, volatility, lower, upper)
            quote_slice = self._build_quote_slice(quote_budget_remaining, remaining_slices, price, stats)
            qty = self._build_buy_quantity(symbol, quote_slice, side_price)
            if qty is None:
                state["last_action"] = "wait"
                return state
            order = self.client.place_limit_buy(symbol, qty, side_price)
            spent = qty * side_price
            state["quote_budget_remaining"] = format(max(Decimal("0"), quote_budget_remaining - spent), "f")
            state["last_action"] = action.lower()
            state["open_order"] = self._compact_order(order)
            LOGGER.info("Placed BUY %s qty=%s price=%s quote_slice=%s price_now=%s", symbol, qty, side_price, spent, price)
            return state

        state["last_action"] = "wait"
        LOGGER.info(
            "Waiting on %s price=%s band=[%s,%s] trend=%s vol=%s position_qty=%s avg_cost=%s budget_remaining=%s",
            symbol,
            price,
            lower,
            upper,
            trend,
            volatility,
            free_base_qty,
            avg_cost,
            quote_budget_remaining,
        )
        return state

    @staticmethod
    def _market_stats(candles: list[list[Any]]) -> dict[str, Decimal]:
        closes = [Decimal(str(candle[4])) for candle in candles if len(candle) >= 5]
        if len(closes) < 2:
            return {"trend": Decimal("0"), "volatility": Decimal("0")}
        returns = []
        for index in range(1, len(closes)):
            previous = closes[index - 1]
            current = closes[index]
            if previous <= 0:
                continue
            returns.append((current / previous) - Decimal("1"))
        if not returns:
            return {"trend": Decimal("0"), "volatility": Decimal("0")}
        trend_window = returns[-5:] if len(returns) >= 5 else returns
        trend = sum(trend_window) / Decimal(len(trend_window))
        volatility = Decimal(str(statistics.pstdev([float(value) for value in returns]))) if len(returns) > 1 else Decimal("0")
        return {"trend": trend, "volatility": volatility}

    def _position_snapshot(self, account: dict[str, Any], symbol: str) -> dict[str, Decimal | None]:
        base_asset = self._symbol_base_asset(symbol)
        balances = {str(item.get("asset")): item for item in account.get("balances", []) if isinstance(item, dict)}
        balance = balances.get(base_asset, {})
        free_base_qty = Decimal(str(balance.get("free", "0")))
        avg_cost = self._rebuild_average_cost(symbol, free_base_qty)
        return {"free_base_qty": free_base_qty, "avg_cost": avg_cost}

    def _rebuild_average_cost(self, symbol: str, current_quantity: Decimal) -> Decimal | None:
        lots: list[list[Decimal]] = []
        base_asset = self._symbol_base_asset(symbol)
        quote_asset = self._symbol_quote_asset(symbol)
        for trade in self.client.get_my_trades(symbol):
            qty = Decimal(str(trade.get("qty", "0")))
            quote_qty = Decimal(str(trade.get("quoteQty", "0")))
            commission = Decimal(str(trade.get("commission", "0")))
            commission_asset = str(trade.get("commissionAsset", "")).upper()
            is_buyer = bool(trade.get("isBuyer"))

            if is_buyer:
                acquired_qty = qty - commission if commission_asset == base_asset else qty
                acquisition_cost = quote_qty + commission if commission_asset == quote_asset else quote_qty
                if acquired_qty > 0:
                    lots.append([acquired_qty, acquisition_cost])
                continue

            sold_qty = qty + commission if commission_asset == base_asset else qty
            remaining_to_remove = sold_qty
            while remaining_to_remove > 0 and lots:
                lot_qty, lot_cost = lots[0]
                if lot_qty <= remaining_to_remove:
                    remaining_to_remove -= lot_qty
                    lots.pop(0)
                    continue
                cost_share = lot_cost * (remaining_to_remove / lot_qty)
                lots[0][0] = lot_qty - remaining_to_remove
                lots[0][1] = lot_cost - cost_share
                remaining_to_remove = Decimal("0")

        remaining_qty = sum((lot[0] for lot in lots), Decimal("0"))
        remaining_cost = sum((lot[1] for lot in lots), Decimal("0"))
        if remaining_qty <= 0 or remaining_cost <= 0:
            return None
        if current_quantity > 0 and remaining_qty > 0:
            return remaining_cost / remaining_qty
        return None

    def _build_buy_quantity(self, symbol: str, quote_slice: Decimal, limit_price: Decimal) -> Decimal | None:
        if quote_slice <= 0:
            return None
        info = self._symbol_filters(symbol)
        raw_qty = quote_slice / limit_price
        qty = self._floor_to_step(raw_qty, info["step_size"])
        if qty <= 0 or qty < info["min_qty"]:
            return None
        if qty * limit_price < info["min_notional"]:
            return None
        return qty

    def _build_sell_quantity(self, free_base_qty: Decimal, remaining_slices: int) -> Decimal | None:
        if free_base_qty <= 0:
            return None
        portion = free_base_qty / Decimal(max(1, remaining_slices))
        qty = self._floor_to_step(portion, self._symbol_filters(self.config.symbol)["step_size"])
        if qty <= 0:
            qty = self._floor_to_step(free_base_qty, self._symbol_filters(self.config.symbol)["step_size"])
        if qty <= 0:
            return None
        return qty

    def _build_quote_slice(self, remaining_quote: Decimal, remaining_slices: int, price: Decimal, stats: dict[str, Decimal]) -> Decimal:
        base_slice = remaining_quote / Decimal(max(1, remaining_slices))
        midpoint = (self.config.lower_price_usdt + self.config.upper_price_usdt) / Decimal("2")
        bias = Decimal("1")
        if price <= midpoint:
            bias += Decimal("0.20")
        else:
            bias -= Decimal("0.20")
        if stats["volatility"] > Decimal("0"):
            bias += min(Decimal("0.20"), stats["volatility"] * Decimal("5"))
        return max(Decimal("0"), min(remaining_quote, base_slice * bias))

    def _build_buy_price(self, price: Decimal, trend: Decimal, volatility: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
        mid = (lower + upper) / Decimal("2")
        pullback = Decimal("0.0012") + min(Decimal("0.004"), volatility * Decimal("6"))
        if trend < 0:
            pullback += min(Decimal("0.002"), abs(trend) * Decimal("12"))
        if price < mid:
            pullback += Decimal("0.0005")
        limit_price = price * (Decimal("1") - pullback)
        return self._clamp(self._tick_floor(limit_price, self._symbol_filters(self.config.symbol)["tick_size"]), lower, upper)

    def _build_sell_price(self, price: Decimal, avg_cost: Decimal | None, trend: Decimal, volatility: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
        markup = Decimal("0.0012") + min(Decimal("0.004"), volatility * Decimal("6"))
        if trend > 0:
            markup += min(Decimal("0.002"), trend * Decimal("12"))
        limit_price = price * (Decimal("1") + markup)
        if avg_cost is not None and avg_cost > 0:
            limit_price = max(limit_price, avg_cost * (Decimal("1") + self.config.profit_take_pct))
        return self._clamp(self._tick_ceil(limit_price, self._symbol_filters(self.config.symbol)["tick_size"]), lower, upper * Decimal("1.05"))

    def _symbol_filters(self, symbol: str) -> dict[str, Decimal]:
        info = self.client.get_exchange_info(symbol)
        symbols = info.get("symbols", [])
        if not symbols:
            raise RuntimeError(f"No exchange filters returned for {symbol}")
        filters = {entry["filterType"]: entry for entry in symbols[0].get("filters", [])}
        lot = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        min_notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        return {
            "step_size": Decimal(str(lot.get("stepSize", "0.00000001"))),
            "min_qty": Decimal(str(lot.get("minQty", "0.00000001"))),
            "tick_size": Decimal(str(price_filter.get("tickSize", "0.01"))),
            "min_notional": Decimal(str(min_notional.get("minNotional", "0"))),
        }

    @staticmethod
    def _symbol_base_asset(symbol: str) -> str:
        return symbol[:-4] if symbol.endswith("USDT") else symbol

    @staticmethod
    def _symbol_quote_asset(symbol: str) -> str:
        return symbol[-4:] if symbol.endswith("USDT") else "USDT"

    def _is_order_stale(self, order: dict[str, Any], state: dict[str, Any]) -> bool:
        placed_at = state.get("open_order", {}).get("placed_at") if isinstance(state.get("open_order"), dict) else None
        if placed_at is None:
            return False
        return (time.time() - float(placed_at)) >= self.config.order_ttl_seconds

    @staticmethod
    def _order_price_drift(order: dict[str, Any], price: Decimal) -> Decimal:
        order_price = Decimal(str(order.get("price", "0")))
        if order_price <= 0:
            return Decimal("0")
        return abs(price - order_price) / order_price

    @staticmethod
    def _compact_order(order: dict[str, Any]) -> dict[str, Any]:
        return {
            "order_id": order.get("orderId"),
            "side": order.get("side"),
            "price": str(order.get("price", "0")),
            "orig_qty": str(order.get("origQty", order.get("quantity", "0"))),
            "status": order.get("status", "NEW"),
            "placed_at": time.time(),
        }

    @staticmethod
    def _clamp(value: Decimal, lower: Decimal, upper: Decimal) -> Decimal:
        return max(lower, min(value, upper))

    @staticmethod
    def _floor_to_step(value: Decimal, step: Decimal) -> Decimal:
        if step <= 0:
            return value
        steps = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return steps * step

    @staticmethod
    def _tick_floor(value: Decimal, tick: Decimal) -> Decimal:
        return AdaptiveTwapRunner._floor_to_step(value, tick)

    @staticmethod
    def _tick_ceil(value: Decimal, tick: Decimal) -> Decimal:
        if tick <= 0:
            return value
        steps = (value / tick).to_integral_value(rounding=ROUND_DOWN)
        floored = steps * tick
        if floored < value:
            floored += tick
        return floored


def _load_env_file(root_dir: Path) -> None:
    env_path = root_dir / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _load_budget_from_strategy(root_dir: Path, fallback: Decimal) -> Decimal:
    strategy_path = root_dir / "strategy.json"
    if not strategy_path.exists():
        return fallback
    try:
        payload = json.loads(strategy_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return fallback
    assets = payload.get("assets", {})
    btc = assets.get("BTC", {}) if isinstance(assets, dict) else {}
    raw_budget = btc.get("reentry_quote_budget_usd") if isinstance(btc, dict) else None
    if raw_budget is None:
        raw_budget = payload.get("default_reentry_quote_budget_usd")
    try:
        return Decimal(str(raw_budget)) if raw_budget is not None else fallback
    except Exception:
        return fallback


def build_config(args: argparse.Namespace) -> LocalTwapConfig:
    root_dir = Path(__file__).resolve().parents[2]
    _load_env_file(root_dir)
    budget = Decimal(str(args.budget)) if args.budget is not None else _load_budget_from_strategy(root_dir, DEFAULT_BUDGET)
    return LocalTwapConfig(
        root_dir=root_dir,
        symbol=str(args.symbol).upper(),
        quote_budget_usdt=budget,
        lower_price_usdt=Decimal(str(args.lower)),
        upper_price_usdt=Decimal(str(args.upper)),
        duration_seconds=int(args.duration_hours * 3600),
        poll_interval_seconds=int(args.poll_seconds),
        profit_take_pct=Decimal(str(args.profit_take_pct)),
        dry_run=bool(args.dry_run),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Adaptive BTCUSDT TWAP bot")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL)
    parser.add_argument("--budget", type=str, default=None)
    parser.add_argument("--lower", type=str, default=str(DEFAULT_LOWER_BOUND))
    parser.add_argument("--upper", type=str, default=str(DEFAULT_UPPER_BOUND))
    parser.add_argument("--duration-hours", type=float, default=8.0)
    parser.add_argument("--poll-seconds", type=int, default=300)
    parser.add_argument("--profit-take-pct", type=str, default=str(DEFAULT_PROFIT_TAKE_PCT))
    parser.add_argument("--live", action="store_true", help="Place live Binance orders instead of dry-run logs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = build_config(argparse.Namespace(**{**vars(args), "dry_run": not args.live}))
    client = BinanceRestClient(
        api_key=os.getenv("BINANCE_API_KEY", "").strip(),
        api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        dry_run=config.dry_run,
    )
    runner = AdaptiveTwapRunner(config, client)
    runner.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
