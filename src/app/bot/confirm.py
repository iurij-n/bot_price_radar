# -*- coding: utf-8 -*-
"""Короткое inline-подтверждение «артикул -> карточка -> да/нет» (§2.5).

Состояния нет: payload живёт в callback_data («add:<nm>», «cancel»).
На «да» данные берутся честно из payload: карточка перезапрашивается у
MarketplaceClient в текущий dest пользователя (цена могла измениться),
затем upsert_card + add_tracking с Ценой сравнения = Финальная цена на
момент подтверждения (ADR-0003). Ответы на callback (answer/edit) —
best-effort: TelegramForbiddenError и прочие ошибки UI-действий глушатся,
результат пользователю доставляется через NotifySender и её DeliveryResult
(«отвечаем через results»), а не через зависший callback.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from app.bot import messages
from app.bot.keyboards import (
    ADD_PREFIX,
    CANCEL_DATA,
    confirm_keyboard,
    decode_add,
    encode_add,
)
from app.domain.limits import can_add
from app.marketplaces.base import CardSnapshot, MarketplaceError
from app.notify.interface import DeliveryResult, OutgoingMessage, UserBlocked
from app.store.repository import DuplicateTrackingError

if TYPE_CHECKING:
    from app.bot.handlers import BotDeps

__all__ = [
    "ADD_PREFIX",
    "CANCEL_DATA",
    "decode_add",
    "encode_add",
    "offer_confirmation",
    "deliver",
    "handle_cancel",
    "handle_confirm",
]


async def offer_confirmation(message, card: CardSnapshot) -> None:
    """Предпросмотр Товара: фото с подписью, fallback — текст с теми же кнопками.

    Фото грузит Telegram по URL (кэш фото сознательно не строим, §6); если
    answer_photo не прошёл (битый/недосягаемый URL) — текстовое сообщение.
    """
    keyboard = confirm_keyboard(card.nm_id, card.link)
    text = messages.card_text(card)
    if card.photo_url:
        try:
            await message.answer_photo(
                photo=card.photo_url, caption=text, reply_markup=keyboard
            )
            return
        except TelegramAPIError:
            pass
    await message.answer(text=text, reply_markup=keyboard)


async def deliver(deps: "BotDeps", chat_id: int, text: str) -> None:
    """Ответ через шов NotifySender; UserBlocked — сигнал: caller глушит активацию."""
    result: DeliveryResult = await deps.sender.send(
        OutgoingMessage(chat_id=chat_id, text=text, button=None)
    )
    if isinstance(result, UserBlocked):
        await deps.store.deactivate_user(chat_id)


async def _best_effort(callback: CallbackQuery, *, text: str | None = None) -> None:
    with contextlib.suppress(TelegramAPIError):
        await callback.answer(text=text)


async def _dismiss_keyboard(callback: CallbackQuery) -> None:
    if callback.message is None:
        return
    with contextlib.suppress(TelegramAPIError):
        await callback.message.edit_reply_markup(reply_markup=None)


async def handle_cancel(callback: CallbackQuery, deps: "BotDeps") -> None:
    """«Нет»: ничего не создаём, гасим кнопки, короткий тост без сообщения."""
    await _best_effort(callback, text=messages.CANCEL_TOAST)
    await _dismiss_keyboard(callback)


async def handle_confirm(callback: CallbackQuery, deps: "BotDeps") -> None:
    """«Да»: перезапрос карточки по nm из payload -> upsert + add_tracking."""
    await _best_effort(callback)
    if callback.from_user is None:
        return
    telegram_id = callback.from_user.id
    chat_id = callback.message.chat.id if callback.message else telegram_id
    nm_id = decode_add(callback.data)
    if nm_id is None:
        await deliver(deps, chat_id, messages.PARSE_NOT_FOUND_HINT)
        await _dismiss_keyboard(callback)
        return

    await deps.store.ensure_user(telegram_id, deps.default_dest)
    dest = await deps.store.get_user_dest(telegram_id) or deps.default_dest
    try:
        batch = await deps.client.fetch_cards_batch([nm_id], dest)
    except MarketplaceError:
        await deliver(deps, chat_id, messages.WB_ERROR_TEXT)
        await _dismiss_keyboard(callback)
        return

    card = next((c for c in batch.cards if c.nm_id == nm_id), None)
    if card is None:
        await deliver(deps, chat_id, messages.PRODUCT_NOT_FOUND_TEXT)
        await _dismiss_keyboard(callback)
        return

    product_id = await deps.store.upsert_card(deps.client.marketplace, card)
    if await deps.store.find_tracking(telegram_id, product_id) is not None:
        await deliver(deps, chat_id, messages.ALREADY_TRACKING_TEXT)
    elif not can_add(
        await deps.store.active_tracking_count(telegram_id), deps.max_active_trackings
    ):
        await deliver(deps, chat_id, messages.limit_reached_text(deps.max_active_trackings))
    else:
        try:
            await deps.store.add_tracking(
                telegram_id, product_id, card.final_price_kop
            )
        except DuplicateTrackingError:
            await deliver(deps, chat_id, messages.ALREADY_TRACKING_TEXT)
        else:
            await deliver(deps, chat_id, messages.added_text(card))
    await _dismiss_keyboard(callback)
