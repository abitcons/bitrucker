# Binance Research — PAXGUSDT — NO TRADE

- Observed at: 2026-11-06T01:17:25Z
- Market data source: Binance Spot public market-data tools (ticker, book ticker, 24h ticker, klines)
- Symbol / market: PAXGUSDT / Spot
- Strategy reference: PAXG-BASELINE-0.1.2 (frozen; not promoted)
- Decision: **NO TRADE**
- Proposal IDs: none
- Validity: this snapshot is descriptive, not an entry signal; refresh before any future decision.

## Existing strategy and prior evidence

The latest available research report before this run was `research/reports/paxg_research_run_20261105_0017.md` (2026-11-05T00:17:53Z). It recorded the frozen PAXG-BASELINE-0.1.2 long-only multi-timeframe trend + RSI pullback strategy:
- Daily EMA50 > EMA200 and 4h EMA50 > EMA200.
- 1h EMA20 > EMA50 and positive MACD histogram.
- 15m RSI crosses above 35, with close above EMA21 and session VWAP.
- Exit: 2x ATR stop, 3x ATR target, trend exit or 24h time exit.
- Backtest: train 1 trade / -0.182%; validation 3 trades / -0.648%; OOS 0 trades; 19 walk-forward windows, 0 positive, 4 total trades. OOS buy-and-hold was +9.695%; the strategy's zero OOS trades are not evidence of edge.
- Historical cost assumptions: 0.10% fee per side, 5 bps slippage per side, 0.5 bps spread per side. These are estimates, not verified account-specific fees or execution costs.

No user-approved mandate file was present. Historical report figures of 5 USDT/trade, 15 USDT/day, and 500 USDT deployment are not treated as approved limits. The checked-in bot `strategy.json` contains `dry_run: false`; this is an application setting, not authorization.

## Current market observations

All values below were returned by Binance market-data tools at 2026-11-06T01:17:25Z unless otherwise stated.

| Metric | Observed value |
|---|---:|
| Last price | 4,087.52 USDT |
| Best bid / ask | 4,087.51 / 4,087.52 |
| Best bid / ask quantity | 1.0988 / 0.8812 PAXG |
| Quoted spread | 0.01 USDT (~0.0245 bps of mid) |
| 24h change | -0.262% |
| 24h high / low | 4,137.21 / 4,061.71 |
| 24h quote volume | 19,767,020.36 USDT |

The displayed best-level quantities are not full depth; no depth/slippage curve was used. Current account fee tier is unknown.

## Closed-candle indicator calculations

Computed from Binance Spot klines, excluding candles whose close time had not passed at observation time. EMA values are recursive EMA calculations over the returned history; ATR is a simple mean of the latest 14 true ranges (an approximation, not the baseline's full Wilder calculation).

| Timeframe | Latest completed candle (UTC) | Close | EMA20 | EMA21 | EMA50 | EMA200 | RSI14 |
|---|---|---:|---:|---:|---:|---:|---:|
| 15m | 01:00 | 4,090.59 | 4,092.19 | 4,092.35 | 4,096.76 | 4,066.02 | 47.75 |
| 1h | 00:00 | 4,089.49 | 4,094.94 | 4,094.41 | 4,064.61 | 4,002.82 | 49.88 |
| 4h | 2026-11-05 20:00 | 4,082.87 | 4,035.09 | 4,032.32 | 4,003.75 | 4,074.12 | 61.33 |
| 1d | 2026-11-05 00:00 | 4,082.87 | 4,034.32 | 4,037.72 | 4,123.18 | 4,358.76 | 52.85 |

Additional 15m values:
- RSI14 previous close: 46.89; latest: 47.75 — no cross above 35.
- Session VWAP: 4,094.02; latest completed close 4,090.59 is below VWAP.
- EMA21: 4,092.35; latest completed close is below EMA21.
- ATR14 approximation: 8.95 USDT.
- Last completed candle volume: 59.7531 PAXG; preceding 20-bar average: 47.0824 PAXG.

## Eligibility assessment

**Mandatory baseline gates fail:**
1. Daily EMA50 (4,123.18) is below EMA200 (4,358.76), so the daily trend filter fails.
2. 4h EMA50 (4,003.75) is below EMA200 (4,074.12), so the 4h trend filter fails.
3. 15m RSI did not cross above 35; it is 47.75 after 46.89.
4. 15m close (4,090.59) is below EMA21 (4,092.35) and session VWAP (4,094.02).
5. 1h EMA20 > EMA50 passes, but positive 1h MACD histogram was not recomputed in this snapshot; this does not override the failed mandatory gates.

No long entry is eligible. The strategy is long-only; no short proposal is allowed. No trade proposal has been created.

## Costs, liquidity, and exposure

- Observed best-quote spread is very tight at approximately 0.0245 bps, but this is only the top quote, not executable depth for a proposed order size.
- 24h quote turnover is approximately 19.77m USDT.
- Historical baseline cost model is 0.10% fee + 5 bps slippage + 0.5 bps spread per side. This remains a backtest assumption, not verified current account pricing.
- No current depth ladder, market-impact estimate, or user-specific fee tier was available in the evidence used.
- Spot only: no funding applies.
- Connected Binance tools exposed public market data only for this run; no private account balances, positions, or open orders were available. Existing exposure is therefore unknown.

## Execution feedback and research integrity

- No execution-result files were found in the new handoff workspace at initialization; there is no verified live/paper execution outcome to compare.
- The previous paper portfolio snapshot recorded 1,000 USDT cash, zero PAXG, zero trades. It is a historical research artifact, not a live account balance.
- Historical backtest losses and the failed/zero-trade validation evidence remain part of the strategy record. No strategy parameters or risk limits were changed.

## Missing information / blockers

1. A user-approved mandate is missing; account capital, risk ceilings, exposure constraints, and permitted execution scope are not verified.
2. Private account read-only access was unavailable; balances, positions, and open orders are unknown.
3. Shared-storage access by the separate implementation agent is not verified. The requested `/shared/binance-trader/` path was absent; this report is saved in the existing repository checkout at `C:\Code\abitcons\bitrucker\trader-research`. Remote synchronization and cross-agent read access must be confirmed before handoff.
4. Account-specific fee tier and order-size market impact are unknown.

## Final decision

**NO TRADE — no proposal emitted.** The frozen long-only baseline fails the daily and 4h trend filters and the 15m pullback confirmation. Missing mandate and unknown account exposure independently prevent an actionable handoff. Do not place, modify, or cancel any order based on this report.
