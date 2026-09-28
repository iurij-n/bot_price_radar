"""Adapter шва NotifySender поверх aiogram Bot (docs/architecture.md §2.4).

Троттлинг ≥1 msg/s между любыми send, FloodWait-ретраи с лимитом попыток,
Forbidden → UserBlocked; наружу не бросает никогда.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.notify.interface import (
    Delivered,
    DeliveryResult,
    Failed,
    OutgoingMessage,
    UserBlocked,
)

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_THROTTLE_INTERVAL = 1.0


class TelegramNotifySender:
    def __init__(
        self,
        bot,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        throttle_interval: float = DEFAULT_THROTTLE_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._bot = bot
        self._max_attempts = max_attempts
        self._throttle_interval = throttle_interval
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._last_send_at: float | None = None

    async def _wait_slot(self) -> None:
        """Вызывается до КАЖДОГО sendMessage: между любыми отправками >= throttle_interval."""
        if self._last_send_at is not None:
            delta = self._clock() - self._last_send_at
            if delta < self._throttle_interval:
                await self._sleep(self._throttle_interval - delta)
        self._last_send_at = self._clock()

    async def send(self, message: OutgoingMessage) -> DeliveryResult:
        attempt = 0
        try:
            reply_markup = None
            rows: list[list[InlineKeyboardButton]] = []
            if message.button is not None:
                label, url = message.button
                rows.append([InlineKeyboardButton(text=label, url=url)])
            if message.callback is not None:
                label, data = message.callback
                rows.append([InlineKeyboardButton(text=label, callback_data=data)])
            if rows:
                reply_markup = InlineKeyboardMarkup(inline_keyboard=rows)
            last_exc: Exception | None = None
            async with self._lock:  # троттлинг — глобальный на экземпляр
                while attempt < self._max_attempts:
                    attempt += 1
                    await self._wait_slot()
                    try:
                        await self._bot.send_message(
                            chat_id=message.chat_id,
                            text=message.text,
                            reply_markup=reply_markup,
                        )
                    except TelegramForbiddenError:
                        return UserBlocked()
                    except TelegramRetryAfter as exc:
                        last_exc = exc
                        if attempt < self._max_attempts:
                            await self._sleep(exc.retry_after)
                        continue
                    return Delivered()
            return Failed(exc_summary=_summarize(last_exc), attempt_count=attempt)
        except Exception as exc:
            # Инвариант шва: наружу не летит ни одна ошибка доставки.
            return Failed(exc_summary=_summarize(exc), attempt_count=attempt)


def _summarize(exc: BaseException | None) -> str:
    if exc is None:
        return "unknown"
    return f"{type(exc).__name__}: {exc}"
