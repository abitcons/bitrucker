from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from .binance_client import BinanceClient, D
from .config import Settings


@dataclass
class AssetSnapshot:
    asset: str
    symbol: str | None
    quantity: Decimal
    free_quantity: Decimal
    locked_quantity: Decimal
    price_usd: Decimal | None
    value_usd: Decimal | None
    avg_cost_usd: Decimal | None
    cost_basis_usd: Decimal | None
    profit_usd: Decimal | None
    profit_pct: Decimal | None
    note: str | None = None


class PortfolioService:
    def __init__(self, client: BinanceClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings
        self._trade_cache: dict[str, tuple[float, list[dict]]] = {}

    def invalidate_trades(self, symbol: str) -> None:
        self._trade_cache.pop(symbol, None)

    def build_snapshots(self) -> list[AssetSnapshot]:
        price_map = self.client.get_all_prices()
        balances = self.client.get_balances()
        snapshots: list[AssetSnapshot] = []
        stable_quote_assets = set(self.settings.stable_quote_assets)

        for balance in balances:
            asset = balance["asset"].upper()
            free_quantity = D(balance["free"])
            locked_quantity = D(balance["locked"])
            quantity = free_quantity + locked_quantity
            if quantity <= 0:
                continue

            symbol = self.settings.assets.get(asset).symbol if asset in self.settings.assets and self.settings.assets[asset].symbol else self.client.find_symbol_for_asset(asset, self.settings.stable_quote_assets)

            price_usd: Decimal | None = None
            value_usd: Decimal | None = None
            avg_cost_usd: Decimal | None = None
            cost_basis_usd: Decimal | None = None
            profit_usd: Decimal | None = None
            profit_pct: Decimal | None = None
            note: str | None = None

            if asset in stable_quote_assets:
                price_usd = Decimal("1")
                value_usd = quantity
                avg_cost_usd = Decimal("1")
                cost_basis_usd = quantity
                profit_usd = Decimal("0")
                profit_pct = Decimal("0")
            elif symbol:
                price_usd = price_map.get(symbol)
                value_usd = quantity * price_usd if price_usd is not None else None
                avg_cost_usd, cost_basis_usd, note = self._rebuild_cost_basis(asset, symbol, quantity)
                if value_usd is not None and cost_basis_usd is not None:
                    profit_usd = value_usd - cost_basis_usd
                    profit_pct = (profit_usd / cost_basis_usd * Decimal("100")) if cost_basis_usd > 0 else None
            else:
                note = "No supported stablecoin symbol found"

            snapshots.append(
                AssetSnapshot(
                    asset=asset,
                    symbol=symbol,
                    quantity=quantity,
                    free_quantity=free_quantity,
                    locked_quantity=locked_quantity,
                    price_usd=price_usd,
                    value_usd=value_usd,
                    avg_cost_usd=avg_cost_usd,
                    cost_basis_usd=cost_basis_usd,
                    profit_usd=profit_usd,
                    profit_pct=profit_pct,
                    note=note,
                )
            )

        snapshots.sort(key=lambda item: item.value_usd or Decimal("0"), reverse=True)
        return snapshots

    def _get_trades(self, symbol: str) -> list[dict]:
        now = time.monotonic()
        cached = self._trade_cache.get(symbol)
        if cached and now - cached[0] < 300:
            return cached[1]
        trades = self.client.get_my_trades(symbol)
        self._trade_cache[symbol] = (now, trades)
        return trades

    def _rebuild_cost_basis(self, asset: str, symbol: str, current_quantity: Decimal) -> tuple[Decimal | None, Decimal | None, str | None]:
        info = self.client.get_symbol(symbol)
        lots: list[list[Decimal]] = []
        for trade in self._get_trades(symbol):
            qty = D(trade["qty"])
            quote_qty = D(trade["quoteQty"])
            commission = D(trade.get("commission", "0"))
            commission_asset = str(trade.get("commissionAsset", "")).upper()

            if trade.get("isBuyer"):
                acquired_qty = qty - commission if commission_asset == info.base_asset else qty
                acquisition_cost = quote_qty + commission if commission_asset == info.quote_asset else quote_qty
                if acquired_qty > 0:
                    lots.append([acquired_qty, acquisition_cost])
                continue

            sold_qty = qty + commission if commission_asset == info.base_asset else qty
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
            return None, None, "No reconstructed long inventory"

        avg_cost = remaining_cost / remaining_qty
        cost_basis = avg_cost * current_quantity
        note: str | None = None
        tolerance = Decimal("0.00000001")
        if current_quantity - remaining_qty > tolerance:
            note = "Current balance exceeds tracked buys; deposit or conversion likely"
            return avg_cost, None, note
        if remaining_qty - current_quantity > tolerance:
            note = "Tracked lots exceed current balance; external outflow likely"
        return avg_cost, cost_basis, note


def _fmt_decimal(value: Decimal | None, decimals: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:.{decimals}f}"


def render_snapshots(snapshots: list[AssetSnapshot]) -> str:
    headers = ["Asset", "Qty", "Value", "Avg Buy", "Cost Basis", "PnL", "PnL %", "Note"]
    rows = [headers]
    for item in snapshots:
        rows.append(
            [
                item.asset,
                _fmt_decimal(item.quantity, 8),
                _fmt_decimal(item.value_usd, 2),
                _fmt_decimal(item.avg_cost_usd, 4),
                _fmt_decimal(item.cost_basis_usd, 2),
                _fmt_decimal(item.profit_usd, 2),
                _fmt_decimal(item.profit_pct, 2),
                item.note or "",
            ]
        )

    widths = [max(len(row[index]) for row in rows) for index in range(len(headers))]
    lines = []
    for row_index, row in enumerate(rows):
        lines.append(" | ".join(value.ljust(widths[index]) for index, value in enumerate(row)))
        if row_index == 0:
            lines.append("-+-".join("-" * width for width in widths))
    return "\n".join(lines)
