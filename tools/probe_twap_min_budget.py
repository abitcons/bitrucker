from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from pathlib import Path

import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from binance_bot.binance_client import BinanceClient
from binance_bot.config import load_settings


def attempt_budget(client: BinanceClient, symbol: str, budget: Decimal, duration_seconds: int) -> dict[str, object]:
    price = client.get_price(symbol)
    quantity = client.validate_quantity(symbol, budget / price, price)
    client_algo_id = uuid.uuid4().hex
    result: dict[str, object] = {
        "symbol": symbol,
        "budget": format(budget, "f"),
        "price": format(price, "f"),
        "quantity": format(quantity, "f"),
        "client_algo_id": client_algo_id,
    }

    try:
        response = client.new_spot_algo_twap(
            symbol=symbol,
            side="BUY",
            quantity=quantity,
            duration_seconds=duration_seconds,
            client_algo_id=client_algo_id,
        )
    except Exception as exc:
        result["accepted"] = False
        result["error"] = str(exc)
        return result

    result["accepted"] = True
    result["response"] = response

    resolved_order: dict | None = None
    resolve_deadline = time.monotonic() + 60
    while time.monotonic() < resolve_deadline:
        resolved_order = client.find_spot_algo_order(symbol=symbol, client_algo_id=client_algo_id)
        if resolved_order is not None:
            break
        time.sleep(2)

    if resolved_order is None:
        result["warning"] = "Accepted but could not resolve the algo order to cancel within 60 seconds."
        return result

    algo_id = int(resolved_order["algoId"])
    result["algo_id"] = algo_id
    result["resolved_status"] = str(resolved_order.get("algoStatus") or "UNKNOWN")

    try:
        client.cancel_spot_algo_order(algo_id)
    except Exception as exc:
        result["warning"] = f"Accepted but cancel failed for algoId={algo_id}: {exc}"
        return result

    final_status = "UNKNOWN"
    cancel_deadline = time.monotonic() + 30
    while time.monotonic() < cancel_deadline:
        latest = client.find_spot_algo_order(symbol=symbol, client_algo_id=client_algo_id, algo_id=algo_id)
        if latest is None:
            final_status = "CANCELLED"
            break
        final_status = str(latest.get("algoStatus") or "UNKNOWN")
        if final_status != "WORKING":
            break
        time.sleep(2)

    result["final_status"] = final_status
    return result


def find_min_budget(
    client: BinanceClient,
    symbol: str,
    duration_seconds: int,
    start_budget: Decimal,
    max_budget: Decimal,
    coarse_step: Decimal,
    fine_step: Decimal,
) -> dict[str, object]:
    attempts: list[dict[str, object]] = []
    last_rejected = start_budget - fine_step
    first_accepted: Decimal | None = None

    budget = start_budget
    while budget <= max_budget:
        attempt = attempt_budget(client, symbol, budget, duration_seconds)
        attempts.append(attempt)
        if bool(attempt.get("accepted")):
            first_accepted = budget
            break
        last_rejected = budget
        budget += coarse_step

    if first_accepted is None:
        return {
            "symbol": symbol,
            "duration_seconds": duration_seconds,
            "min_accepted_budget": None,
            "attempts": attempts,
        }

    min_accepted = first_accepted
    fine_budget = last_rejected + fine_step
    while fine_budget < first_accepted:
        attempt = attempt_budget(client, symbol, fine_budget, duration_seconds)
        attempts.append(attempt)
        if bool(attempt.get("accepted")):
            min_accepted = fine_budget
            break
        fine_budget += fine_step

    return {
        "symbol": symbol,
        "duration_seconds": duration_seconds,
        "min_accepted_budget": format(min_accepted, "f"),
        "attempts": attempts,
    }


def main() -> None:
    settings = load_settings(ROOT_DIR)
    client = BinanceClient(settings.api_key, settings.api_secret, dry_run=False)

    duration_seconds = settings.default_twap_duration_seconds
    symbols = ("BTCUSDT", "ETHUSDT")
    results: list[dict[str, object]] = []

    for symbol in symbols:
        result = find_min_budget(
            client=client,
            symbol=symbol,
            duration_seconds=duration_seconds,
            start_budget=Decimal("500"),
            max_budget=Decimal("5000"),
            coarse_step=Decimal("250"),
            fine_step=Decimal("50"),
        )
        results.append(result)
        print(json.dumps(result, indent=2))

    print(json.dumps({"results": results}, indent=2))


if __name__ == "__main__":
    main()