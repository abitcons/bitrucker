from __future__ import annotations

import argparse
import logging
from pathlib import Path

from .bootstrap import build_strategy
from .local_twap import main as local_twap_main


def main() -> None:
    parser = argparse.ArgumentParser(description="Binance profit-cycle bot")
    parser.add_argument(
        "command",
        choices=["summary", "run", "dashboard", "local-twap"],
        help="Run a one-time summary, the continuous loop, the web dashboard, or the standalone BTC recovery TWAP",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard host binding")
    parser.add_argument("--port", type=int, default=8000, help="Dashboard port binding")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol for local-twap mode")
    parser.add_argument("--budget", default=None, help="Quote budget in USDT for local-twap mode")
    parser.add_argument("--lower", default="50000", help="Lower price bound for local-twap mode")
    parser.add_argument("--upper", default="60000", help="Upper price bound for local-twap mode")
    parser.add_argument("--duration-hours", type=float, default=8.0, help="Runtime window in hours for local-twap mode")
    parser.add_argument("--poll-seconds", type=int, default=300, help="Polling cadence in seconds for local-twap mode")
    parser.add_argument("--profit-take-pct", default="0.02", help="Profit-taking markup for local-twap mode")
    parser.add_argument("--live", action="store_true", help="Place live Binance orders in local-twap mode")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    root_dir = Path(__file__).resolve().parents[2]
    strategy = build_strategy(root_dir)

    try:
        if args.command == "summary":
            strategy.print_summary()
            return
        if args.command == "dashboard":
            from .dashboard import create_dashboard_app

            app = create_dashboard_app(root_dir)
            app.run(host=args.host, port=args.port, debug=False)
            return
        if args.command == "local-twap":
            local_twap_args = [
                "--symbol",
                args.symbol,
                "--lower",
                args.lower,
                "--upper",
                args.upper,
                "--duration-hours",
                str(args.duration_hours),
                "--poll-seconds",
                str(args.poll_seconds),
                "--profit-take-pct",
                args.profit_take_pct,
            ]
            if args.budget is not None:
                local_twap_args.extend(["--budget", args.budget])
            if args.live:
                local_twap_args.append("--live")
            local_twap_main(local_twap_args)
            return
        strategy.run_forever()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()