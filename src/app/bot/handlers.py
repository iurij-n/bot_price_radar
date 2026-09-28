# -*- coding: utf-8 -*-
"""Тонкие handlers: ввод -> domain/Store -> форматирование (§2.5).

Запрещено и отсутствует: SQL, httpx, бизнес-решения (пороги, 48ч, лимит —
через domain-функции). Зависимости приходят в BotDeps из композиции
(main.py), handler-функции отделены от регистрации в Router — тесты зовут
их напрямую с duck-typed фейками.

Единственный интерфейс персистентности — app.store.repository.Store:
читалка get_user_dest добавлена в него (тикет 06, композиционный разрыв
убран), локальный подпротокол BotStore больше не нужен.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from typing import Mapping

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from app.bot import messages
from app.bot.confirm import (
    ADD_PREFIX,
    CANCEL_DATA,
    deliver,
    handle_cancel,
    handle_confirm,
    offer_confirmation,
)
from app.bot.keyboards import (
    DEL_LABEL,
    DEL_PREFIX,
    OPEN_LINK_LABEL,
    REGION_PREFIX,
    decode_del,
    decode_region,
    encode_del,
    region_keyboard,
)
from app.domain.limits import can_add
from app.domain.parsing import extract_sku
from app.marketplaces.base import MarketplaceError
from app.marketplaces.base import MarketplaceClient
from app.notify.interface import Failed, NotifySender, OutgoingMessage, UserBlocked
from app.store.repository import Store

logger = logging.getLogger("app.bot.handlers")


@dataclass(frozen=True)
class BotDeps:
    store: Store
    client: MarketplaceClient
    sender: NotifySender
    default_dest: str
    max_active_trackings: int
    regions: Mapping[str, str]  # справочник город->dest из config (тикеты 05/07)


async def handle_start(message: Message, deps: BotDeps) -> None:
    telegram_id = message.from_user.id
    await deps.store.ensure_user(telegram_id, deps.default_dest)
    await deps.store.reactivate_user(telegram_id)
    await deliver(deps, message.chat.id, messages.START_TEXT)


async def handle_help(message: Message, deps: BotDeps) -> None:
    await deliver(deps, message.chat.id, messages.HELP_TEXT)


async def handle_new_product(message: Message, deps: BotDeps) -> None:
    """Текст/ссылка -> extract_sku -> карточка в dest -> предпросмотр с ✅/✖."""
    telegram_id = message.from_user.id
    chat_id = message.chat.id

    parse = extract_sku(message.text or "")
    if parse.reason == "short_link":
        await deliver(deps, chat_id, messages.SHORT_LINK_HINT)
        return
    if parse.reason == "ambiguous":
        await deliver(deps, chat_id, messages.AMBIGUOUS_HINT)
        return
    if parse.nm_id is None:
        await deliver(deps, chat_id, messages.PARSE_NOT_FOUND_HINT)
        return

    await deps.store.ensure_user(telegram_id, deps.default_dest)
    dest = await deps.store.get_user_dest(telegram_id) or deps.default_dest
    try:
        batch = await deps.client.fetch_cards_batch([parse.nm_id], dest)
    except MarketplaceError:
        await deliver(deps, chat_id, messages.WB_ERROR_TEXT)
        return

    card = next((c for c in batch.cards if c.nm_id == parse.nm_id), None)
    if card is None:
        await deliver(deps, chat_id, messages.PRODUCT_NOT_FOUND_TEXT)
        return

    product_id = await deps.store.upsert_card(deps.client.marketplace, card)
    if await deps.store.find_tracking(telegram_id, product_id) is not None:
        await deliver(deps, chat_id, messages.ALREADY_TRACKING_TEXT)
        return
    if not can_add(
        await deps.store.active_tracking_count(telegram_id), deps.max_active_trackings
    ):
        await deliver(deps, chat_id, messages.limit_reached_text(deps.max_active_trackings))
        return

    await offer_confirmation(message, card)


async def handle_list(message: Message, deps: BotDeps) -> None:
    """/list: текстовое сообщение на каждое Отслеживание через NotifySender.

    Фото здесь нет сознательно: seam NotifySender (§2.4) умеет только
    текст+кнопки. Троттлинг (≥1 msg/s) — свойство шва, отдельного
    rate-limit в handler нет.
    UserBlocked — сигнал: деактивируем и останавливаем выдачу (пользователь
    всё равно больше не читает); Failed — лог и продолжение.
    """
    telegram_id = message.from_user.id
    chat_id = message.chat.id
    rows = await deps.store.list_trackings(telegram_id)
    if not rows:
        await deliver(deps, chat_id, messages.LIST_EMPTY_TEXT)
        return
    for row in rows:
        result = await deps.sender.send(
            OutgoingMessage(
                chat_id=chat_id,
                text=messages.tracking_text(row),
                button=(OPEN_LINK_LABEL, row.link),
                callback=(DEL_LABEL, encode_del(row.product_id)),
            )
        )
        if isinstance(result, UserBlocked):
            logger.warning(
                "/list: пользователь %s заблокировал бота — выдача остановлена",
                telegram_id,
            )
            await deps.store.deactivate_user(telegram_id)
            break
        if isinstance(result, Failed):
            logger.warning(
                "/list: доставка товара %s не удалась: %s", row.nm_id, result.exc_summary
            )


async def handle_delete(callback: CallbackQuery, deps: BotDeps) -> None:
    """🗑 (del:<product_id>): remove_tracking + перезапись сообщения.

    remove_tracking идемпотентен и молчит на неизвестной паре — ответ
    «Удалено» даём всегда. edit/answer — best-effort: ошибки Telegram
    глушим, статус операции уже доставлен через edit-сообщение.
    """
    with contextlib.suppress(TelegramAPIError):
        await callback.answer()
    if callback.from_user is None:
        return
    product_id = decode_del(callback.data)
    if product_id is None:
        return
    await deps.store.remove_tracking(callback.from_user.id, product_id)
    message = callback.message
    if message is None:
        return
    with contextlib.suppress(TelegramAPIError):
        await message.edit_text(messages.DELETED_TEXT)
    with contextlib.suppress(TelegramAPIError):
        await message.edit_reply_markup(reply_markup=None)


async def handle_region(message: Message, deps: BotDeps) -> None:
    """/region: inline-клавиатура справочника Регионов из config, текущий — с ✓.

    Клавиатура грузится через message.answer напрямую (у NotifySender нет
    произвольных клавиатур, §2.4) — это UI-ответ на команду, не Уведомление.
    """
    telegram_id = message.from_user.id
    current = await deps.store.get_user_dest(telegram_id) or deps.default_dest
    await message.answer(
        messages.REGION_PROMPT_TEXT,
        reply_markup=region_keyboard(deps.regions, current_dest=current),
    )


def _region_name(regions: Mapping[str, str], dest: str) -> str:
    """Имя города по dest из справочника (коллизию разрешает детерминированно)."""
    for city in sorted(regions):
        if regions[city] == dest:
            return city
    return dest


async def handle_region_choose(callback: CallbackQuery, deps: BotDeps) -> None:
    """reg:<dest>: смена Региона с молчаливым сбросом Цены сравнения (ADR-0003).

    Никаких Уведомлений: ответы идут через callback (тост + правка
    клавиатуры), NotifySender не задействуется. Ошибка маркетплейса —
    регион НЕ меняется (dest пользователя остаётся прежним). Пустой список
    Отслеживаний — set_region без перезапроса цен (пользователь без подписок
    тоже может сменить регион).
    """
    if callback.from_user is None:
        return
    telegram_id = callback.from_user.id
    new_dest = decode_region(callback.data)
    if new_dest is None:
        with contextlib.suppress(TelegramAPIError):
            await callback.answer()
        return

    current = await deps.store.get_user_dest(telegram_id) or deps.default_dest
    if new_dest == current:
        with contextlib.suppress(TelegramAPIError):
            await callback.answer(text=messages.REGION_SAME_TEXT)
        return

    nms = [row.nm_id for row in await deps.store.list_trackings(telegram_id)]
    prices: dict[int, int | None] = {}
    if nms:
        try:
            batch = await deps.client.fetch_cards_batch(nms, new_dest)
        except MarketplaceError:
            logger.warning(
                "/region: пользователь %s — ошибка маркетплейса на dest %r, "
                "регион не изменён",
                telegram_id,
                new_dest,
                exc_info=True,
            )
            with contextlib.suppress(TelegramAPIError):
                await callback.answer(text=messages.REGION_CHECK_FAILED_TEXT)
            return
        cards_by_nm = {card.nm_id: card for card in batch.cards}
        prices = {
            nm: cards_by_nm[nm].final_price_kop if nm in cards_by_nm else None
            for nm in nms
        }

    await deps.store.set_region(telegram_id, new_dest, prices)
    with contextlib.suppress(TelegramAPIError):
        await callback.answer(
            text=messages.region_changed_text(_region_name(deps.regions, new_dest))
        )
    if callback.message is not None:
        with contextlib.suppress(TelegramAPIError):
            await callback.message.edit_reply_markup(
                reply_markup=region_keyboard(deps.regions, current_dest=new_dest)
            )


def register_handlers(router: Router, deps: BotDeps) -> None:
    """Вешает handlers на Router; deps замыкается, глобального состояния нет."""

    @router.message(CommandStart())
    async def _start(message: Message) -> None:
        await handle_start(message, deps)

    @router.message(Command("help"))
    async def _help(message: Message) -> None:
        await handle_help(message, deps)

    @router.message(Command("list"))
    async def _list(message: Message) -> None:
        await handle_list(message, deps)

    @router.message(Command("region"))
    async def _region(message: Message) -> None:
        await handle_region(message, deps)

    @router.callback_query(F.data.startswith(REGION_PREFIX))
    async def _region_choose(callback: CallbackQuery) -> None:
        await handle_region_choose(callback, deps)

    @router.callback_query(F.data.startswith(DEL_PREFIX))
    async def _delete(callback: CallbackQuery) -> None:
        await handle_delete(callback, deps)

    @router.callback_query(F.data == CANCEL_DATA)
    async def _cancel(callback: CallbackQuery) -> None:
        await handle_cancel(callback, deps)

    @router.callback_query(F.data.startswith(ADD_PREFIX))
    async def _confirm(callback: CallbackQuery) -> None:
        await handle_confirm(callback, deps)

    @router.message(F.text & ~F.text.startswith("/"))
    async def _product(message: Message) -> None:
        await handle_new_product(message, deps)
