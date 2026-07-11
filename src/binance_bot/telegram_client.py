from __future__ import annotations

from typing import Any

import requests

from .config import Settings


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.enabled = bool(settings.notifications_enabled and settings.telegram_api_key and settings.telegram_chat_id)
        self._api_key = settings.telegram_api_key
        self._chat_id = settings.telegram_chat_id

    @property
    def chat_id(self) -> str | None:
        return self._chat_id

    def send(self, message: str) -> None:
        if not self.enabled:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self._api_key}/sendMessage",
                json={"chat_id": self._chat_id, "text": message[:4096]},
                timeout=15,
            ).raise_for_status()
        except requests.RequestException:
            return

    def get_updates(self, offset: int | None = None, timeout_seconds: int = 0) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        params: dict[str, object] = {"timeout": max(0, timeout_seconds)}
        if offset is not None:
            params["offset"] = offset
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{self._api_key}/getUpdates",
                params=params,
                timeout=max(15, timeout_seconds + 5),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError):
            return []
        if not isinstance(payload, dict) or not payload.get("ok"):
            return []
        result = payload.get("result", [])
        if not isinstance(result, list):
            return []
        return [item for item in result if isinstance(item, dict)]
