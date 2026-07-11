#!/usr/bin/env powershell
# Quick launcher script for the Volatility-Triggered TWAP Bot
# Usage: .\run_bot.ps1

Write-Host "🚀 Starting Binance Volatility TWAP Bot..." -ForegroundColor Green
Write-Host "━" * 60 -ForegroundColor Cyan

# Check if running from correct directory
if (-not (Test-Path "launch_with_tunnel.py")) {
    Write-Host "❌ ERROR: Run this script from c:\Code\Imran\binance_bot" -ForegroundColor Red
    exit 1
}

# Check if .env exists and has credentials
if (-not (Test-Path ".env")) {
    Write-Host "❌ ERROR: .env file not found" -ForegroundColor Red
    exit 1
}

$envContent = Get-Content ".env" -Raw
if ($envContent -notmatch "BINANCE_API_KEY") {
    Write-Host "❌ ERROR: .env missing BINANCE_API_KEY" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "✅ Configuration verified" -ForegroundColor Green
Write-Host ""
Write-Host "📋 Bot Settings:" -ForegroundColor Cyan
Write-Host "  • Budget: $5,000 USDT"
Write-Host "  • Duration: 12 hours"
Write-Host "  • Profit Target: $10 USD (fixed)"
Write-Host "  • Volatility Threshold: 2%"
Write-Host "  • Mode: DRY-RUN (simulated)"
Write-Host ""
Write-Host "🌐 Starting Cloudflare Tunnel..." -ForegroundColor Cyan
Write-Host "━" * 60 -ForegroundColor Cyan

# Start the bot with tunnel
python launch_with_tunnel.py --dashboard-port 8000 --tunnel-name binance-bot

# Cleanup
Write-Host ""
Write-Host "━" * 60 -ForegroundColor Cyan
Write-Host "🛑 Bot stopped" -ForegroundColor Yellow
