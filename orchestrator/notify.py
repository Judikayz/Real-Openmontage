"""Optional Telegram notifications."""
from __future__ import annotations

import httpx


def telegram_request(cfg, text: str):
    return (f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            {"chat_id": cfg.telegram_chat_id, "text": text[:3500]})


def notify(cfg, text: str, transport: httpx.BaseTransport | None = None) -> bool:
    if not (cfg.telegram_bot_token and cfg.telegram_chat_id):
        return False
    url, body = telegram_request(cfg, text)
    try:
        with httpx.Client(timeout=10, transport=transport) as c:
            return c.post(url, json=body).status_code == 200
    except httpx.HTTPError:
        return False
