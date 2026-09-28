# -*- coding: utf-8 -*-
"""Inline-клавиатуры и кодекс callback_data UI-слоя (§2.5).

Только сборка клавиатур и кодирование payload кнопок; бизнес-решений нет.
Клавиатура Регионов — чистая функция от словаря город->dest из config
(тикеты 05/07): handler передаёт settings.regions, хардкода городов нет.
"""

from __future__ import annotations

from typing import Mapping

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CONFIRM_ADD_LABEL = "✅ Добавить"
CONFIRM_CANCEL_LABEL = "✖"
OPEN_LINK_LABEL = "🔗 Открыть товар"
DEL_LABEL = "🗑 Удалить"

ADD_PREFIX = "add:"
DEL_PREFIX = "del:"
CANCEL_DATA = "cancel"
REGION_PREFIX = "reg:"
_CALLBACK_LIMIT = 64  # лимит Telegram на длину callback_data


def encode_add(nm_id: int) -> str:
    data = f"{ADD_PREFIX}{nm_id}"
    if len(data.encode("utf-8")) > _CALLBACK_LIMIT:
        raise ValueError(f"callback_data длиннее {_CALLBACK_LIMIT} байт: {data!r}")
    return data


def encode_del(product_id: int) -> str:
    data = f"{DEL_PREFIX}{product_id}"
    if len(data.encode("utf-8")) > _CALLBACK_LIMIT:
        raise ValueError(f"callback_data длиннее {_CALLBACK_LIMIT} байт: {data!r}")
    return data


def decode_add(data: str | None) -> int | None:
    if not data or not data.startswith(ADD_PREFIX):
        return None
    tail = data[len(ADD_PREFIX) :]
    return int(tail) if tail.isdigit() else None


def decode_del(data: str | None) -> int | None:
    if not data or not data.startswith(DEL_PREFIX):
        return None
    tail = data[len(DEL_PREFIX) :]
    return int(tail) if tail.isdigit() else None


def encode_region(dest: str) -> str:
    data = f"{REGION_PREFIX}{dest}"
    if len(data.encode("utf-8")) > _CALLBACK_LIMIT:
        raise ValueError(f"callback_data длиннее {_CALLBACK_LIMIT} байт: {data!r}")
    return data


def decode_region(data: str | None) -> str | None:
    if not data or not data.startswith(REGION_PREFIX):
        return None
    tail = data[len(REGION_PREFIX) :]
    # dest — opaque-строка справочника config (бывает отрицательным числом)
    return tail or None


def confirm_keyboard(nm_id: int, link: str) -> InlineKeyboardMarkup:
    """Подтверждение добавления: ✅ (callback add:<nm>) / ✖ + ссылка на карточку."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=CONFIRM_ADD_LABEL, callback_data=encode_add(nm_id)
                ),
                InlineKeyboardButton(
                    text=CONFIRM_CANCEL_LABEL, callback_data=CANCEL_DATA
                ),
            ],
            [InlineKeyboardButton(text=OPEN_LINK_LABEL, url=link)],
        ]
    )


def region_keyboard(
    regions: Mapping[str, str], current_dest: str | None = None
) -> InlineKeyboardMarkup:
    """Клавиатура выбора Региона из справочника config (город -> dest), тикет 07.

    Текущий dest подсвечен префиксом «✓ » в подписи кнопки — чистая UI-метка,
    не бизнес-решение.
    """
    rows = []
    for city in sorted(regions):
        dest = regions[city]
        label = f"✓ {city}" if dest == current_dest else city
        rows.append([InlineKeyboardButton(text=label, callback_data=encode_region(dest))])
    return InlineKeyboardMarkup(inline_keyboard=rows)
