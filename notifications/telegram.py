"""
Telegram notifications for bot events.
"""

import asyncio
import html as html_mod
from datetime import datetime, timezone
from typing import Optional

import httpx
from loguru import logger

import config


class TelegramNotifier:
    """Sends Telegram messages for bot events."""

    API_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
    ):
        self.bot_token = bot_token or config.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID
        self._client: Optional[httpx.AsyncClient] = None
        self.enabled = bool(self.bot_token and self.chat_id)

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
        return self._client

    async def send(self, message: str, parse_mode: str = "HTML"):
        """Send a message to the configured Telegram chat."""
        if not self.enabled:
            logger.debug(f"Telegram disabled, would send: {message[:100]}")
            return

        client = await self._get_client()
        url = self.API_URL.format(token=self.bot_token)

        try:
            resp = await client.post(
                url,
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", "unknown")
            logger.error(f"Telegram send failed: {type(e).__name__} (status: {status})")

    # --- Convenience methods ---

    async def bot_started(self, version: str = "1.0.0"):
        mode = "DRY RUN" if config.DRY_RUN else "LIVE"
        await self.send(
            f"🤖 <b>Bot Started</b>\n"
            f"Mode: <code>{mode}</code>\n"
            f"Version: <code>{version}</code>\n"
            f"Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )

    async def position_opened(
        self, question: str, side: str, price: float, size: float, strategy: str
    ):
        safe_q = html_mod.escape(question[:100])
        await self.send(
            f"📈 <b>Position Opened</b>\n"
            f"Market: {safe_q}\n"
            f"Side: <code>{side}</code>\n"
            f"Price: <code>{price:.4f}</code>\n"
            f"Size: <code>${size:.2f}</code>\n"
            f"Strategy: <code>{strategy}</code>"
        )

    async def position_closed(
        self, question: str, pnl: float, pnl_pct: float, reason: str = ""
    ):
        safe_q = html_mod.escape(question[:100])
        safe_reason = html_mod.escape(reason or "manual")
        emoji = "✅" if pnl >= 0 else "❌"
        await self.send(
            f"{emoji} <b>Position Closed</b>\n"
            f"Market: {safe_q}\n"
            f"P&L: <code>${pnl:+.2f}</code> ({pnl_pct:+.1f}%)\n"
            f"Reason: {safe_reason}"
        )

    async def daily_stop_loss(self, total_loss: float, capital_remaining: float):
        await self.send(
            f"🚨 <b>DAILY STOP-LOSS TRIGGERED</b>\n"
            f"Total loss today: <code>${total_loss:.2f}</code>\n"
            f"Capital remaining: <code>${capital_remaining:.2f}</code>\n"
            f"All trading halted until tomorrow."
        )

    async def circuit_breaker(self, reason: str, resume_time: str):
        safe_reason = html_mod.escape(reason)
        await self.send(
            f"⚠️ <b>Circuit Breaker Activated</b>\n"
            f"Reason: {safe_reason}\n"
            f"Resume: <code>{resume_time}</code>"
        )

    async def critical_error(self, error_type: str, trace: str):
        safe_type = html_mod.escape(error_type)
        safe_trace = html_mod.escape(trace[:500])
        await self.send(
            f"🔴 <b>Critical Error</b>\n"
            f"Type: <code>{safe_type}</code>\n"
            f"<pre>{safe_trace}</pre>"
        )

    async def daily_report(
        self,
        capital: float,
        day_pnl: float,
        open_positions: int,
        trades_count: int,
        win_rate: float,
    ):
        await self.send(
            f"📊 <b>Daily Report</b> — "
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n\n"
            f"Capital: <code>${capital:.2f}</code>\n"
            f"Day P&L: <code>${day_pnl:+.2f}</code>\n"
            f"Open positions: <code>{open_positions}</code>\n"
            f"Trades today: <code>{trades_count}</code>\n"
            f"Win rate: <code>{win_rate:.0%}</code>"
        )

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
