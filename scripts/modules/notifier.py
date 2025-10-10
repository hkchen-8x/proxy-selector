"""Telegram notification sender"""

from __future__ import annotations

import logging
from typing import Optional

from .config import TelegramSettings


class TelegramNotifier:
    def __init__(self, settings: Optional[TelegramSettings]) -> None:
        self._settings = settings

    async def send_alert(self, message: str) -> None:
        if not self._settings or not self._settings.enabled:
            logging.debug("Telegram 通知未启用")
            return

        url = f"https://api.telegram.org/bot{self._settings.bot_token}/sendMessage"
        payload = {
            "chat_id": self._settings.chat_id,
            "text": message,
            "parse_mode": "HTML",
        }

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as response:
                    if response.status == 200:
                        logging.info("Telegram 告警发送成功")
                    else:
                        error_text = await response.text()
                        logging.error("Telegram 告警发送失败: %s - %s", response.status, error_text)
        except ImportError:
            logging.error("Telegram 通知需要安装 aiohttp: pip install aiohttp")
        except Exception as exc:
            logging.error("Telegram 告警发送异常: %s", exc)

