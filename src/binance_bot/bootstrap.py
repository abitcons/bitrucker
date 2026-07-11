from __future__ import annotations

from pathlib import Path

from .binance_client import BinanceClient
from .bitget_client import BitgetClient
from .config import load_settings
from .portfolio import PortfolioService
from .state import StateStore
from .strategy import TradingStrategy
from .telegram_client import TelegramNotifier


def build_strategy(root_dir: Path) -> TradingStrategy:
    settings = load_settings(root_dir)
    client = BinanceClient(settings.api_key, settings.api_secret, settings.dry_run)
    portfolio = PortfolioService(client, settings)
    notifier = TelegramNotifier(settings)
    state = StateStore(settings.state_file)

    bitget: BitgetClient | None = None
    if settings.bitget_enabled:
        bitget = BitgetClient(
            api_key=settings.bitget_api_key,
            api_secret=settings.bitget_api_secret,
            passphrase=settings.bitget_passphrase,
            dry_run=settings.dry_run,
        )

    return TradingStrategy(settings, client, portfolio, notifier, state, bitget=bitget)