# Remote Dashboard Access via Cloudflare Tunnel

This workspace includes a Cloudflare Tunnel launcher that exposes your local bot dashboard to the public internet securely.

## Setup

1. **Download cloudflared** (one-time):
   ```powershell
   $url = 'https://github.com/cloudflare/cloudflared/releases/download/2024.12.0/cloudflared-windows-amd64.exe'
   $dest = 'C:\Program Files\cloudflared\cloudflared.exe'
   New-Item -ItemType Directory -Path 'C:\Program Files\cloudflared' -Force > $null
   iwr -Uri $url -OutFile $dest
   ```

2. **Start the dashboard and named tunnel**:
   ```powershell
   python -m src.binance_bot dashboard --host 127.0.0.1 --port 8000
   & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel --config .cloudflared\config.yml run binance-bot
   ```

3. **Access remotely**:
   - Open `https://binance-bot.abit.ai/bot` in any browser from anywhere
   - The dashboard is live and you can control your bot remotely

## Security Notes

- **Cloudflare Tunnel is secure by default**: Traffic is encrypted end-to-end through Cloudflare's network
- **No port forwarding needed**: No need to expose your home IP
- **Session-based auth**: The dashboard uses CSRF tokens for form submissions
- **Rotate secrets**: The `.env` file with Binance keys is NOT exposed through the web; only the dashboard runs remotely

## Running the Local TWAP bot

While the dashboard tunnel is running, you can also run the BTCUSDT recovery bot in a separate terminal:

```powershell
python main.py local-twap --symbol BTCUSDT --lower 50000 --upper 60000 --duration-hours 8
```

Then monitor it from the remote dashboard at `https://binance-bot.abit.ai/bot`.

## Stopping the Tunnel

Press `Ctrl+C` in the tunnel launcher terminal to cleanly shut down both the dashboard and tunnel.
