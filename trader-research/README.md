# Binance Trader Research Handoff

Workspace: `C:\Code\abitcons\bitrucker\trader-research`
Repository: `https://github.com/abitcons/bitrucker`
Strategy reference: repository `strategy.json` and existing PAXG research under `research/reports/`.

## Shared-storage status
The requested `/shared/binance-trader/` path does not exist on Development Server Dev-02. This repository-backed directory is the fallback workspace, not a verified shared mount. It is in the existing BitRucker Git checkout and can be shared through the repository once committed and synchronized. **Cross-agent read access and remote synchronization have not been verified.** The implementation agent must not assume it can see uncommitted local files; confirm the same repository/branch and synchronize this directory before relying on it. No private agent storage has been substituted or represented as shared.

## Folder structure
- `research/` — immutable UTC-stamped market research reports.
- `proposals/` — immutable JSON trade proposals; only actionable candidates that pass the frozen strategy and mandate checks.
- `execution/` — implementation agent outcomes; research agent reads only.
- `reviews/` — expected-versus-actual analysis and lessons.
- `control/` — user-approved mandate and operating controls. Research agent must not create or modify approvals.

## Strategy scope and controls
- Current research continuation: PAXGUSDT Binance Spot, frozen baseline `PAXG-BASELINE-0.1.2`.
- The baseline is long-only: daily and 4h EMA50 > EMA200; 1h EMA20 > EMA50 and positive MACD histogram; 15m RSI crosses above 35, close above EMA21 and session VWAP. Exits: 2x ATR stop, 3x ATR target, trend exit or 24h time exit.
- Historical backtest found only four train/validation trades, all losing; zero OOS trades; zero positive walk-forward windows. Baseline is not evidence of a profitable edge.
- Existing repository `strategy.json` has `dry_run: false`; that application setting is not a user-approved research mandate and is not authorization for this research handoff.
- No verified user-approved mandate was present at initialization. See `control/MANDATE_STATUS.md`. Do not invent or infer risk limits, account allocation, fee tier, or approval.
- Research is read-only. Never place, modify, or cancel orders. A proposal is not authorization to trade.
- Never store API keys, secrets, passwords, or authentication tokens.

## Handoff procedure
1. Read this README, control files, latest research reports, all open proposals, and latest execution/review records before each run.
2. Obtain fresh, timestamped Binance market data; identify closed candles only.
3. Separate observed data, calculations, assumptions, and forecasts. Include fee, spread, liquidity, slippage, and (for derivatives) funding.
4. Record WAIT/NO TRADE when any mandatory condition fails or mandate/account information is unavailable.
5. Write reports to a temporary file and atomically rename into `research/`. Create proposals only for eligible candidates, using temporary JSON plus atomic rename. Never rewrite a published proposal; supersede it explicitly.
6. Implementation agent must independently validate proposal expiry, strategy version, mandate, and current conditions. Proposal status `proposed` never means approved.

## Initial state
- No verified user-approved mandate found.
- No execution-result or review records found in this research workspace at creation.
- No account balances, positions, or open orders were available through the connected read-only Binance tools.
- Initial current-market decision: NO TRADE (see timestamped report).
