"""Interface шва NotifySender (docs/architecture.md §2.4).

Значения и Protocol: вызывающий (bot/worker) передаёт OutgoingMessage и всегда
получает DeliveryResult, никогда не ловит исключение. UserBlocked — сигнал, а не
действие: деактивацию выполняет caller через Store.deactivate_user.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutgoingMessage:
    chat_id: int
    text: str
    button: tuple[str, str] | None  # (label, url), напр. ("Открыть товар", ссылка)
    # необязательная callback-кнопка (label, callback_data) отдельной строкой —
    # нужна /list (🗑 «del:<id>»); по умолчанию None, существующие вызовы не трогает
    callback: tuple[str, str] | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """База иерархии результатов доставки; наружу отдаются только подклассы."""


@dataclass(frozen=True)
class Delivered(DeliveryResult):
    """Сообщение доставлено."""


@dataclass(frozen=True)
class UserBlocked(DeliveryResult):
    """Пользователь заблокировал бота (TelegramForbidden) — Неактивный пользователь."""


@dataclass(frozen=True)
class Failed(DeliveryResult):
    """Доставка не удалась после всех попыток; исключение не выпускается наружу."""

    exc_summary: str
    attempt_count: int


class NotifySender(Protocol):
    async def send(self, message: OutgoingMessage) -> DeliveryResult: ...
