# Binance Profit Cycle Bot

> **Private beta:** this repository now powers the BitRucker multi-asset trading agent, including Binance, Bitget crypto, and supported Bitget tokenized market instruments. It is not yet approved for public multi-customer live trading.

The fail-closed production gate is available at `/health/ready`; owner/admin users can inspect the full **Launch Readiness** scorecard in the application. See [MARKET_READINESS.md](MARKET_READINESS.md) for the competitor benchmark, critical blockers, product rules, and implementation roadmap.

Customer fleets now have isolated credentials, runtime state, pair limits, and paper execution. Customer live orders remain disabled; the existing live fleet is owner-only. A durable emergency switch blocks new entries without disabling stop/close and protective-exit paths. No automated strategy is guaranteed or foolproof.

This project began as a spot trading loop for Binance with Telegram notifications.

The bot does four things:

1. Reads your current spot balances.
2. Rebuilds each asset's buy price from historical fills instead of using Binance's average price.
3. Tracks profit across the managed strategy assets as a whole.
4. When the managed strategy reaches the configured exit threshold, it stops every managed TWAP, sells managed holdings, and starts the participating allowed strategies over.

## Strategy Refinement

Your requested strategy was refined into an explicit and safer execution model:

1. Only trade assets listed in `strategy.json` or assets you already hold with a supported stablecoin symbol.
2. Rebuild cost basis from historical fills using FIFO lots. This handles partial sells and avoids relying on Binance's reported average entry.
3. The managed assets are treated as one strategy basket for automatic exits.
4. A managed basket exit now requires both the configured USD profit and the configured profit percent threshold, which reduces churn from tiny favorable moves.
5. Auto-start only happens when a managed asset is effectively flat. The bot will not average into an existing managed position just because a TWAP is idle.
6. When combined unrealized profit across the managed assets reaches the configured exit threshold, the bot cancels the managed Spot Algo Orders, sells the managed holdings, and restarts the participating allowed strategies.
7. The bot monitors the exchange-managed TWAP orders, their sub-orders, and your live positions rather than scheduling local slices itself.

For development, keep IIS synced directly to this workspace:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_iis_dev_sync_task.ps1
```

Run it from an elevated PowerShell prompt once. It installs the `BinanceBotIisDevSync` scheduled task, which watches this workspace and recycles the IIS app pool when Python, template, CSS, JavaScript, JSON, or config files change. IIS serves the files directly from `C:\Code\Imran\binance_bot`, so what you edit here is what IIS runs.
8. Manual stop, allow, and sell controls still work per asset.
9. Every meaningful event can be pushed to Telegram.
10. Each managed asset can be paused or allowed independently from Telegram and the dashboard.

## Files

- `strategy.json`: Strategy and per-asset settings.
- `.env`: Exchange and Telegram credentials. Keep this private.
- `runtime/state.json`: Persistent Spot Algo TWAP cycle state created automatically.

## Setup

1. Create and activate a Python environment.
2. Install dependencies:

```powershell
pip install -r requirements.txt
```

3. Keep your existing `.env` and add these optional variables if needed:

```dotenv
NOTIFICATIONS_ENABLED=true
DASHBOARD_URL=http://127.0.0.1:8000/
```

4. Review `strategy.json` carefully.
5. Leave `dry_run` as `true` until you are satisfied with the summary output. The checked-in `strategy.json` now defaults to `dry_run: true`.

## IIS Development Hosting

Deploy the app to IIS from an elevated PowerShell prompt:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\deploy_iis.ps1
```

IIS serves the workspace directly from `C:\Code\Imran\binance_bot`; there is no separate copied publish folder. To keep IIS aligned while developing, install the dev sync watcher once from an elevated PowerShell prompt:

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\install_iis_dev_sync_task.ps1
```

That installs the `BinanceBotIisDevSync` scheduled task. It watches Python, template, CSS, JavaScript, JSON, and config files in this workspace and recycles the IIS app pool after changes, so the IIS app uses the same code you are editing.

## Usage

Show a one-time portfolio summary:

```powershell
python main.py summary
```

Run the continuous bot:

```powershell
python main.py run
```

Run the standalone BTCUSDT recovery TWAP bot for an 8-hour window:

```powershell
python main.py local-twap --symbol BTCUSDT --lower 50000 --upper 60000 --duration-hours 8 --poll-seconds 300
```

Use `--live` only after your Binance key has Spot trading permission and Spot Algo access. Otherwise the bot stays in dry-run mode and logs its decisions.

Run the local dashboard:

```powershell
python main.py dashboard --host 127.0.0.1 --port 8000
```

Telegram control commands:

```text
/status
/page
/portfolio
/portfolio 12000
/portfolio off
/help
/start BTC
/start ETH
/start PAXG
/allow PAXG
/allow BTC
/allow ETH
/stop PAXG
/stop BTC
/stop ETH
```

Plain Telegram text commands are also accepted, for example:

```text
start btc
start eth
stop btc
sell paxg
portfolio 12000
portfolio off
status
```

## Important Notes

- The bot only supports spot assets that have a direct stablecoin symbol such as `BTCUSDT`.
- Market orders are used for exits. Entries are submitted through Binance Spot Algo Orders TWAP.
- Commission handling is included in the cost-basis reconstruction when the fee is charged in the base or quote asset.
- `dry_run=true` still fetches live balances and prices, but it does not place live orders.
- Native Spot Algo Orders require Binance API trading permission and account support for Spot Algo Orders.
- The dashboard is local by default and provides manual sell, stop, and allow controls for each managed asset.
- The dashboard also provides a manual Start action for managed assets that are idle or paused.
- The dashboard also lets you change the whole-strategy USD profit trigger at runtime without editing `strategy.json`. Any configured profit-percent threshold still applies.
- The dashboard also lets you arm a portfolio-value liquidation trigger. When total portfolio value reaches that USD amount, the bot pauses all managed assets, cancels managed TWAPs, and sells every sellable non-USDT asset into USDT.
- Telegram `/page` and `/status` return the same high-level status as the dashboard, including an overall strategy status summary and the configured dashboard URL.
- Telegram `/portfolio <usd>` arms the same full-liquidation trigger, and `/portfolio off` clears it.
- If you want to open the dashboard from Telegram on another device, expose the dashboard on a reachable HTTPS URL and set `DASHBOARD_URL` or `dashboard_url` accordingly.
- `profit_target_usd` is the whole managed-strategy profit trigger, not a per-asset managed exit target.
- `profit_target_pct` adds a percentage-based confirmation so the bot does not churn out of the basket on an insignificant dollar move.
- `/stop <asset>` pauses that asset, cancels its TWAP if needed, sells the asset, and prevents restarts until `/allow <asset>` is issued.
- This repository should not store your secrets in version control.
