# -*- coding: utf-8 -*-
"""Тексты и форматирование UI-слоя (docs/architecture.md §2.5).

Только строки и чистое форматирование значений; никаких I/O и решений.
Цены приходят из швов целыми копейками (int) и здесь превращаются в рубли.
"""

from __future__ import annotations

from app.marketplaces.base import CardSnapshot
from app.store.repository import TrackingRow

START_TEXT = (
    "👋 Привет! Я Price Radar — слежу за финальными ценами на Wildberries "
    "и уведомляю, когда цена заметно меняется.\n\n"
    "Как добавить товар:\n"
    "1. Пришли ссылку на карточку или 6–10-значный артикул.\n"
    "2. Я покажу товар и его цену в твоём регионе.\n"
    "3. Нажми ✅ «Добавить» — и я начну следить за ценой.\n\n"
    "Команды: /help — справка, /start — перезапуск."
)

HELP_TEXT = (
    "📖 Справка Price Radar\n\n"
    "Команды:\n"
    "/start — приветствие и включение отслеживаний\n"
    "/help — этот список\n\n"
    "Как добавить товар: пришли одну из вещей —\n"
    "• полную ссылку вида wildberries.ru/catalog/<артикул>/detail.aspx "
    "или /product-card/<артикул>;\n"
    "• или просто артикул — 6–10 цифр.\n\n"
    "Короткие ссылки go.wb.ru я сознательно не раскрываю — прислай "
    "артикул или полную ссылку.\n\n"
    "Бот отслеживает финальную цену (с учётом скидок) в твоём регионе."
)

PARSE_NOT_FOUND_HINT = (
    "Не распознал сообщение 🤔 Пришли артикул товара (6–10 цифр) "
    "или полную ссылку на карточку wildberries.ru."
)

SHORT_LINK_HINT = (
    "Это короткая ссылка go.wb.ru — артикул из неё не извлекается, "
    "раскрывать редиректы я сознательно не умею. Открой товар в "
    "браузере и пришли артикул (6–10 цифр) или полную ссылку вида "
    "wildberries.ru/catalog/<артикул>/detail.aspx."
)

AMBIGUOUS_HINT = (
    "В сообщении несколько разных чисел — угадывать артикул не буду, "
    "легко добавить не тот товар. Пришли ровно один артикул (6–10 "
    "цифр) или ссылку на конкретную карточку."
)

PRODUCT_NOT_FOUND_TEXT = (
    "Товар не найден 😕 WB не вернул карточку по этому артикулу — "
    "проверь, что номер выбран правильно и товар существует."
)

ALREADY_TRACKING_TEXT = (
    "Товар уже в отслеживании — дубликат не создаю. Отслеживание "
    "настроено на его текущую цену, новых уведомлений о добавлении не будет."
)

WB_ERROR_TEXT = (
    "Wildberries сейчас не отвечает (сеть/блокировка/таймаут) — "
    "попробуй ещё раз через минуту. Товар не добавлен."
)

UNAVAILABLE_NOTE = "Сейчас нет цены в вашем регионе"

CANCEL_TOAST = "Отменено — товар не добавлен"

ADDED_NO_PRICE_SUFFIX = (
    "Сейчас у товара нет цены в твоём регионе — как только появится, "
    "пришлю уведомление."
)

LIST_EMPTY_TEXT = (
    "Список пуст — пришли артикул (6–10 цифр) или ссылку на товар, "
    "и я начну следить за его ценой."
)

DELETED_TEXT = (
    "🗑 Удалено из отслеживания — уведомления по этому товару больше "
    "не придут."
)

REGION_PROMPT_TEXT = (
    "🌍 Выбор региона. Текущий отмечен ✓.\n\n"
    "При смене региона я молча переставлю базу сравнения на текущие цены "
    "в новом регионе — ложных уведомлений из-за региональной разницы цен "
    "не будет."
)

REGION_SAME_TEXT = "Этот регион уже выбран."

REGION_CHECK_FAILED_TEXT = (
    "Не удалось проверить цены в новом регионе, попробуй позже — регион не изменён."
)


def limit_reached_text(limit: int) -> str:
    return (
        f"Достигнут лимит: {limit} активных отслеживаний на пользователя. "
        "Удали ненужные — и освободится место для новых товаров."
    )


def region_changed_text(name: str) -> str:
    """Подтверждение смены Региона (тикет 07) — короткий тост, не Уведомление."""
    return f"Регион изменён на {name}."


def format_price_kop(price_kop: int) -> str:
    """Копейки (int) -> «1 234,56 ₽»; дробные копейки показываются, целые — нет."""
    if price_kop < 0:
        raise ValueError("цена в копейках не может быть отрицательной")
    rub, kop = divmod(price_kop, 100)
    rub_text = f"{rub:,}".replace(",", "\u00a0")
    if kop == 0:
        return f"{rub_text}\u00a0₽"
    return f"{rub_text},{kop:02d}\u00a0₽"


def card_text(card: CardSnapshot) -> str:
    """Подпись предпросмотра Товара: название, артикул, Финальная цена/статус."""
    name = card.name or f"Товар {card.nm_id}"
    if card.final_price_kop is None:
        price_line = UNAVAILABLE_NOTE
    else:
        price_line = f"Цена: {format_price_kop(card.final_price_kop)}"
    return f"{name}\nАртикул: {card.nm_id}\n{price_line}"


def tracking_text(row: TrackingRow) -> str:
    """Строка /list: название, Артикул, Финальная цена в Регионе пользователя.

    current_price_kop is None покрывает оба случая (нет строки product_prices
    и Недоступный товар) одним текстом «нет цены в вашем регионе» — различать
    их TrackingRow не может, отдельного сигнала нет.
    """
    name = row.name or f"Товар {row.nm_id}"
    if row.current_price_kop is None:
        price_line = UNAVAILABLE_NOTE
    else:
        price_line = f"Цена: {format_price_kop(row.current_price_kop)}"
    return f"{name}\nАртикул: {row.nm_id}\n{price_line}"


def added_text(card: CardSnapshot) -> str:
    """Подтверждение успешного добавления Отслеживания."""
    name = card.name or f"Товар {card.nm_id}"
    if card.final_price_kop is None:
        return f"✅ Добавил «{name}» в отслеживание.\n{ADDED_NO_PRICE_SUFFIX}"
    return (
        f"✅ Добавил «{name}» в отслеживание.\n"
        f"Цены отслеживаю от {format_price_kop(card.final_price_kop)} — "
        "уведомлю, когда цена изменится заметно."
    )
