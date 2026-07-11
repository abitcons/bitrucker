╔════════════════════════════════════════════════════════════════════════════╗
║                      CLOUDFLARE TUNNEL CUSTOM DOMAIN SETUP                 ║
║                    (binance-bot.abit.ai → Bot Dashboard)                    ║
╚════════════════════════════════════════════════════════════════════════════╝

PREREQUISITE: You must have abit.ai domain in Cloudflare
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

If you already have abit.ai managed in Cloudflare, skip to STEP 1.

If NOT, you need to:
1. Go to https://www.cloudflare.com
2. Sign up / Log in
3. Add abit.ai domain
4. Update nameservers at your domain registrar to point to Cloudflare


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 1: Authenticate Cloudflared with Your Cloudflare Account
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

In PowerShell, run:

  cd c:\Code\Imran\binance_bot
  & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" login

This will:
1. Open a browser window asking you to log in to Cloudflare
2. Ask permission to create a tunnel
3. Save credentials to ~/.cloudflared/cert.pem
4. Return to terminal with success message


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 2: Create the Tunnel
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

In PowerShell, run:

  & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel create binance-bot

This will:
1. Create a tunnel named "binance-bot"
2. Show you a Tunnel ID
3. Save the tunnel credentials
4. Print: "Created tunnel binance-bot with ID xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 3: Route Traffic from Your Domain
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

In PowerShell, run:

  & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel route dns binance-bot binance-bot.abit.ai

This will:
1. Create DNS CNAME records automatically in Cloudflare
2. Point binance-bot.abit.ai → Cloudflare Tunnel


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 4: Start the Bot with Custom Domain
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

In PowerShell, run:

  cd c:\Code\Imran\binance_bot
  & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel run binance-bot

This will:
1. Start the tunnel using the config file (.cloudflared/config.yml)
2. Route traffic from binance-bot.abit.ai to localhost:8000
3. Keep running in the terminal

NOTE: You need ANOTHER terminal to start the Flask dashboard!


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 5: Start the Flask Dashboard (In a NEW Terminal)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

In a NEW PowerShell window, run:

  cd c:\Code\Imran\binance_bot
  python main.py dashboard --host 127.0.0.1 --port 8000

Or use the Flask runner directly:

  python -c "from src.binance_bot.dashboard import create_dashboard_app; from pathlib import Path; app = create_dashboard_app(Path.cwd()); app.run(host='127.0.0.1', port=8000, debug=True)"


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STEP 6: Access Your Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Open your browser and go to:

  https://binance-bot.abit.ai/bot

Your bot dashboard will load with full functionality!


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TERMINAL SETUP (Summary)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Terminal 1 - Cloudflare Tunnel:
  cd c:\Code\Imran\binance_bot
  & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel run binance-bot
  [Stays running, DON'T close]

Terminal 2 - Flask Dashboard:
  cd c:\Code\Imran\binance_bot
  python main.py dashboard --port 8000
  [Stays running, DON'T close]

Browser:
  Open: https://binance-bot.abit.ai/bot
  [Access your bot from anywhere]


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
QUICK COMMANDS (Copy-Paste Ready)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 1. Authenticate (one-time setup)
& "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" login

# 2. Create tunnel (one-time setup)
& "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel create binance-bot

# 3. Route domains (one-time setup)
& "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel route dns binance-bot binance-bot.abit.ai

# 4. Run tunnel (every time)
& "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel run binance-bot

# 5. Run dashboard (every time, in different terminal)
cd c:\Code\Imran\binance_bot && python main.py dashboard --port 8000

# 6. Access bot
# Open browser: https://binance-bot.abit.ai/bot


━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TROUBLESHOOTING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

❌ "Domain doesn't resolve"
   ✓ Wait 5 minutes for DNS propagation
   ✓ Verify tunnel route was created:
     & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" tunnel list
   ✓ Check Cloudflare dashboard → DNS records

❌ "Connection refused"
   ✓ Make sure Flask dashboard is running in Terminal 2
   ✓ Check Flask is listening on port 8000:
     netstat -ano | findstr :8000

❌ "404 error on domain"
   ✓ Verify config.yml exists: .cloudflared/config.yml
   ✓ Check tunnel credentials: ~/.cloudflared/binance-bot.json
   ✓ Restart tunnel: Kill Terminal 1, run Step 4 again

❌ "Invalid credentials"
   ✓ Run login again:
     & "$env:LOCALAPPDATA\cloudflared\cloudflared.exe" login
   ✓ Delete ~/.cloudflared/cert.pem and try again

═══════════════════════════════════════════════════════════════════════════════
