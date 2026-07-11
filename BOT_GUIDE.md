# Volatility-Triggered TWAP Bot

## Overview

The **Volatility-Triggered TWAP Bot** is an intelligent trading bot that:

- 🔍 **Monitors market volatility** in real-time
- 🚀 **Auto-enters when volatility spikes** above configurable threshold
- 📊 **Uses adaptive TWAP** for smooth order execution
- 💰 **Takes profit at fixed 10 USDT** target (not percentage-based)
- 🎮 **Controlled via modern web dashboard** with real-time updates
- 🌐 **Publicly accessible** via Cloudflare Tunnel
- 🔄 **Auto-restarts** after profit target or idle conditions

## Quick Start

### 1. **Access the Dashboard with Tunnel**

```powershell
cd c:\Code\Imran\binance_bot
python launch_with_tunnel.py --dashboard-port 8000 --tunnel-name binance-bot
```

The script will print a public URL like:
```
🌐 Public dashboard URL: https://binance-bot-abc123.cfd.sh
```

### 2. **Open the Bot Dashboard**

Open your tunnel URL and click the **Bot Dashboard** link, or navigate directly to:
```
https://binance-bot-abc123.cfd.sh/bot
```

### 3. **Start Trading**

On the dashboard:
1. **Monitor Volatility** - Watch the volatility percentage and threshold meter
2. **Click "Start Trading"** - Bot begins monitoring for volatility spike
3. **Auto-Entry** - When volatility exceeds 2%, bot automatically enters
4. **Position Tracking** - Real-time P&L and position monitoring
5. **Auto-Exit** - At $10 USDT profit, bot sells and completes cycle

## Dashboard Features

### Status Panel
- **Bot State**: Displays current mode (idle, waiting, trading, completed, error)
- **Cycles Completed**: Number of successful trading cycles
- **Current Price**: Live BTCUSDT price

### Market Volatility Panel
- **Current Volatility**: 30-candle 1-minute volatility (%)
- **Threshold**: Entry volatility requirement (default 2%)
- **Visual Indicator**: Colored bar showing volatility level
  - 🟢 Green: Below threshold
  - 🟡 Yellow: Near threshold
  - 🔴 Red: Above threshold (ready to enter)

### Position Panel
- **Holdings (BTC)**: Quantity of BTC in position
- **Entry Price**: Price at which position was entered
- **Cost Basis**: Total USDT invested in position

### Profit Tracking Panel
- **Current P&L**: Real-time profit/loss (labeled in red or green)
- **Target Profit**: $10.00 (fixed)
- **Progress Bar**: Visual representation of profit progress to target

### Budget Panel
- **Total Budget**: $5000 allocated for this cycle
- **Remaining**: Uninvested USDT available
- **Duration**: 12-hour trading window

### Controls
- **Start Trading**: Begin volatility monitoring (enabled when status is idle)
- **Stop Bot**: Halt current trading cycle (enabled during trading)
- **Restart**: Start a new cycle after completion (enabled after trading ends)

## Configuration

The bot runs with default settings:
- **Budget**: $5000 per cycle
- **Duration**: 12 hours
- **Volatility Threshold**: 2% (30-candle, 1-minute)
- **Profit Target**: $10 USD (fixed, not percentage)
- **Mode**: DRY-RUN (simulated, no real orders)

### To Edit Settings

Open the dashboard page source or modify programmatically:

```python
from src.binance_bot.volatility_bot import VolatilityTwapBot

bot = VolatilityTwapBot(root_dir, api_key, api_secret, dry_run=True)
bot.state.target_profit_usdt = Decimal("15")  # Change target to $15
bot.state.volatility_threshold = 0.025  # Change threshold to 2.5%
bot._save_state()
```

## Trading Flow

```
┌─────────────────────────────────────┐
│   IDLE / WAITING STATE              │
│  ✓ Click "Start Trading"            │
│  ✓ Bot monitors volatility          │
└─────────────────────────────────────┘
                ⬇️
┌─────────────────────────────────────┐
│   VOLATILITY SPIKE DETECTED 📈      │
│  ✓ Volatility > 2% threshold        │
│  ✓ Automatically enters position    │
└─────────────────────────────────────┘
                ⬇️
┌─────────────────────────────────────┐
│   TRADING STATE                     │
│  ✓ Holding BTC position             │
│  ✓ Real-time P&L tracking           │
│  ✓ Monitoring for profit target     │
└─────────────────────────────────────┘
                ⬇️
┌─────────────────────────────────────┐
│   PROFIT TARGET HIT 🎯              │
│  ✓ Position closed at $10 profit    │
│  ✓ Cycle marked "completed"         │
│  ✓ Ready for manual restart         │
└─────────────────────────────────────┘
                ⬇️
┌─────────────────────────────────────┐
│   CLICK "RESTART" FOR NEW CYCLE     │
│  ✓ Reset state to idle              │
│  ✓ Cycle counter incremented        │
│  ✓ New budget allocated             │
└─────────────────────────────────────┘
```

## Security

### Cloudflare Tunnel
- ✅ **End-to-end encryption** via Cloudflare network
- ✅ **No home IP exposure** (no port forwarding)
- ✅ **CSRF protection** on all forms (Flask session tokens)
- ✅ **API key never transmitted** (stays in `.env` on server)

### Best Practices
1. **Keep tunnel URL private** - Don't share publicly
2. **Rotate API keys regularly** - Delete old keys from Binance
3. **Monitor activity** - Check bot logs for unusual behavior
4. **Use read-only mode first** - Test with dry-run before live trading

## Troubleshooting

### Bot won't start
- Check `.env` file has valid `BINANCE_API_KEY` and `BINANCE_API_SECRET`
- Verify API key has Spot Trading enabled on Binance
- Check browser console for errors: Press F12 → Console tab

### Dashboard won't load
- Verify tunnel is running: Look for "🌐 Public dashboard URL" message
- Check URL is correct (should show Cloudflare URL)
- Refresh page (Ctrl+Shift+R for hard refresh)

### Volatility stuck at 0%
- Network issue: Check internet connection
- Binance API rate limit: Wait a few minutes
- Check browser console for API errors

### Position not updating
- Dashboard updates every 2 seconds
- Check bot is actually running (status should show "trading")
- Verify API credentials are valid

## Advanced Usage

### Run in Background
```powershell
# Start tunnel in background
Start-Process powershell -ArgumentList "cd c:\Code\Imran\binance_bot; python launch_with_tunnel.py"

# Access dashboard from anywhere
```

### API Endpoints (JSON)
```bash
# Get bot status
curl https://binance-bot-abc.cfd.sh/api/bot/status

# Start bot
curl -X POST https://binance-bot-abc.cfd.sh/api/bot/start \
  -H "Content-Type: application/json" \
  -d '{"budget": 5000, "duration_hours": 12}'

# Stop bot
curl -X POST https://binance-bot-abc.cfd.sh/api/bot/stop

# Restart bot
curl -X POST https://binance-bot-abc.cfd.sh/api/bot/restart
```

## File Locations

- **State File**: `runtime/bot_state.json` (persistent state between sessions)
- **Dashboard**: `src/binance_bot/templates/bot_dashboard.html`
- **Bot Code**: `src/binance_bot/volatility_bot.py`
- **Flask Integration**: `src/binance_bot/dashboard.py`
- **Config**: `.env` (API keys, keep secret!)

## Next Steps

1. ✅ **Test with dry-run** (current mode)
   - Start bot, watch it monitor volatility
   - Verify all dashboard features work

2. 🔐 **Enable live trading** (when ready)
   - Set `dry_run=False` in code
   - Rotate to a new, limited API key for trading
   - Start with small budget ($100-500)

3. 📊 **Monitor and optimize**
   - Adjust volatility threshold based on market
   - Change profit target based on your risk tolerance
   - Track ROI over time

## Support

- **Logs**: Check terminal output for bot activity
- **State**: Check `runtime/bot_state.json` for detailed state
- **API Docs**: https://binance-docs.github.io/apidocs/

---

**Happy trading!** 🚀
