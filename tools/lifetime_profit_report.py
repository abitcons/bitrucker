from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.binance_bot.config import load_settings

BINANCE_BASE_URL = "https://api.binance.com"


@dataclass
class SymbolReport:
    symbol: str
    trades: int
    buys: int
    sells: int
    buy_quote: Decimal
    sell_quote: Decimal
    fees_quote_est: Decimal
    realized_pnl: Decimal
    inventory_qty: Decimal
    inventory_cost: Decimal
    mark_price: Decimal
    position_value: Decimal
    unrealized_pnl: Decimal
    net_pnl: Decimal


def _send_signed_request(api_key: str, api_secret: str, method: str, path: str, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
    query = urllib.parse.urlencode(params, quote_via=urllib.parse.quote, safe="")
    ts = int(time.time() * 1000)
    query += f"&timestamp={ts}&recvWindow=5000"
    sig = hmac.new(api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{BINANCE_BASE_URL}{path}?{query}&signature={sig}"
    req = urllib.request.Request(
        url,
        data=b"" if method in {"POST", "DELETE"} else None,
        headers={"X-MBX-APIKEY": api_key, "User-Agent": "profit-report"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _send_public_request(path: str, params: dict[str, Any]) -> dict[str, Any] | list[Any]:
    query = urllib.parse.urlencode(params)
    url = f"{BINANCE_BASE_URL}{path}?{query}" if query else f"{BINANCE_BASE_URL}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "profit-report"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _load_symbols(root_dir: Path) -> list[str]:
    managed = root_dir / "runtime" / "managed_pairs.json"
    if managed.exists():
        payload = json.loads(managed.read_text(encoding="utf-8"))
        pairs = payload.get("pairs")
        if isinstance(pairs, list):
            items = [str(item).upper().replace("/", "").strip() for item in pairs if str(item).strip()]
            return sorted(set(items))
    return ["PAXGUSDT"]


def _fetch_all_trades(api_key: str, api_secret: str, symbol: str) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    from_id: int | None = None
    while True:
        params: dict[str, Any] = {"symbol": symbol, "limit": 1000}
        if from_id is not None:
            params["fromId"] = from_id
        page = _send_signed_request(api_key, api_secret, "GET", "/api/v3/myTrades", params)
        if not isinstance(page, list) or not page:
            break
        trades.extend(page)
        if len(page) < 1000:
            break
        from_id = int(page[-1].get("id", 0)) + 1
        time.sleep(0.05)
    return sorted(trades, key=lambda t: (int(t.get("time", 0)), int(t.get("id", 0))))


def _report_for_symbol(api_key: str, api_secret: str, symbol: str) -> SymbolReport:
    base_asset = symbol[:-4] if symbol.endswith("USDT") else symbol
    trades = _fetch_all_trades(api_key, api_secret, symbol)

    inventory_qty = Decimal("0")
    inventory_cost = Decimal("0")
    realized_pnl = Decimal("0")
    buy_quote = Decimal("0")
    sell_quote = Decimal("0")
    fees_quote_est = Decimal("0")
    buys = 0
    sells = 0

    for trade in trades:
        qty = Decimal(str(trade.get("qty", "0")))
        quote_qty = Decimal(str(trade.get("quoteQty", "0")))
        commission = Decimal(str(trade.get("commission", "0")))
        commission_asset = str(trade.get("commissionAsset", ""))
        is_buy = bool(trade.get("isBuyer"))

        fee_quote_equiv = Decimal("0")
        if commission_asset == "USDT":
            fee_quote_equiv = commission
        elif commission_asset == base_asset:
            price = Decimal(str(trade.get("price", "0")))
            fee_quote_equiv = commission * price

        fees_quote_est += fee_quote_equiv

        if is_buy:
            buys += 1
            net_qty = qty - commission if commission_asset == base_asset else qty
            trade_cost = quote_qty + (commission if commission_asset == "USDT" else Decimal("0"))
            inventory_qty += max(net_qty, Decimal("0"))
            inventory_cost += trade_cost
            buy_quote += quote_qty
        else:
            sells += 1
            sold_qty = qty + commission if commission_asset == base_asset else qty
            if inventory_qty > 0 and sold_qty > 0:
                avg_cost = inventory_cost / inventory_qty
                cost_released = avg_cost * sold_qty
                proceeds = quote_qty - (commission if commission_asset == "USDT" else Decimal("0"))
                realized_pnl += proceeds - cost_released
                inventory_qty -= sold_qty
                inventory_cost -= cost_released
                if inventory_qty <= Decimal("0.0000000001"):
                    inventory_qty = Decimal("0")
                    inventory_cost = Decimal("0")
            sell_quote += quote_qty

    ticker = _send_public_request("/api/v3/ticker/price", {"symbol": symbol})
    mark_price = Decimal(str((ticker if isinstance(ticker, dict) else {}).get("price", "0")))
    position_value = (inventory_qty * mark_price).quantize(Decimal("0.00000001"))
    unrealized_pnl = (position_value - inventory_cost).quantize(Decimal("0.00000001"))
    net_pnl = (realized_pnl + unrealized_pnl).quantize(Decimal("0.00000001"))

    return SymbolReport(
        symbol=symbol,
        trades=len(trades),
        buys=buys,
        sells=sells,
        buy_quote=buy_quote,
        sell_quote=sell_quote,
        fees_quote_est=fees_quote_est,
        realized_pnl=realized_pnl.quantize(Decimal("0.00000001")),
        inventory_qty=inventory_qty.quantize(Decimal("0.00000001")),
        inventory_cost=inventory_cost.quantize(Decimal("0.00000001")),
        mark_price=mark_price,
        position_value=position_value,
        unrealized_pnl=unrealized_pnl,
        net_pnl=net_pnl,
    )


def main() -> None:
    root_dir = Path(__file__).resolve().parents[1]
    settings = load_settings(root_dir)
    symbols = _load_symbols(root_dir)

    reports = [_report_for_symbol(settings.api_key, settings.api_secret, symbol) for symbol in symbols]

    total_realized = sum((r.realized_pnl for r in reports), Decimal("0"))
    total_unrealized = sum((r.unrealized_pnl for r in reports), Decimal("0"))
    total_net = sum((r.net_pnl for r in reports), Decimal("0"))

    print("LIFETIME PROFIT REPORT (Binance myTrades based)")
    print("=" * 72)
    for r in reports:
        print(
            f"{r.symbol}: trades={r.trades} buys={r.buys} sells={r.sells} | "
            f"realized={r.realized_pnl:.8f} USDT | unrealized={r.unrealized_pnl:.8f} USDT | net={r.net_pnl:.8f} USDT"
        )
        print(
            f"  inventory={r.inventory_qty:.8f} @ mark {r.mark_price:.8f} => value {r.position_value:.8f} USDT, "
            f"inventory_cost={r.inventory_cost:.8f}, fees_est={r.fees_quote_est:.8f}"
        )

    print("-" * 72)
    print(f"TOTAL realized:   {total_realized:.8f} USDT")
    print(f"TOTAL unrealized: {total_unrealized:.8f} USDT")
    print(f"TOTAL net:        {total_net:.8f} USDT")


if __name__ == "__main__":
    main()
