# -*- coding: utf-8 -*-
"""Handler-тесты /region и reg:<dest>-колбэка на fake-швах (тикет 07).

Тонкий слой: клавиатура строится из config-справочника (не хардкод), выбор
города -> fetch карточек в НОВЫЙ dest -> Store.set_region с dict цен
(None для Недоступного/отсутствующего) -> молчаливый сброс Цены сравнения
(ADR-0003) покрывается semantics самого set_region (tests/store), здесь —
правильность аргументов. Ни одного Уведомления: sender.sent пуст на всех
ветках. Ошибка маркетплейса — set_region НЕ вызван, регион не изменён.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.bot import messages
from app.bot.handlers import BotDeps, handle_region, handle_region_choose
from app.bot.keyboards import REGION_PREFIX, decode_region, encode_region
from app.marketplaces.base import BatchResult, CardSnapshot, MarketplaceError
from app.notify.interface import Delivered, OutgoingMessage
from app.store.repository import TrackingRow

USER_ID = 900
CHAT_ID = 900
KURSK = "4"
MOSCOW = "7"
BELGOROD = "-123"  # dest — строка, бывает отрицательным
REGIONS = {"Курск": KURSK, "Москва": MOSCOW, "Белгород": BELGOROD}


# ------------------------------- фейки швов -------------------------------


def make_row(product_id: int, nm_id: int, *, price: int | None = 10000) -> TrackingRow:
    return TrackingRow(
        subscription_id=product_id,
        product_id=product_id,
        marketplace="wb",
        nm_id=nm_id,
        name=f"Товар {nm_id}",
        photo_url=None,
        link=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        dest=KURSK,
        current_price_kop=price,
        comparison_price_kop=price,
        status="active",
    )


def make_card(nm_id: int, price: int | None) -> CardSnapshot:
    return CardSnapshot(
        nm_id=nm_id,
        name=f"Товар {nm_id}",
        photo_url=None,
        link=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        final_price_kop=price,
        available=price is not None,
    )


class RegionStore:
    """Dict-based fake Store (тот же Interface): dest, Отслеживания, set_region."""

    def __init__(self, dest: str | None = KURSK, rows: tuple[TrackingRow, ...] = ()):
        self.dest = dest
        self.rows = rows
        self.region_calls: list[tuple[int, str, dict[int, int | None]]] = []

    async def get_user_dest(self, telegram_id: int) -> str | None:
        return self.dest

    async def list_trackings(self, telegram_id: int) -> tuple[TrackingRow, ...]:
        return self.rows

    async def set_region(
        self, telegram_id: int, dest: str, prices: dict[int, int | None]
    ) -> None:
        # как SqlStore: смена dest + молчаливой сброс Цены сравнения
        self.region_calls.append((telegram_id, dest, dict(prices)))
        self.dest = dest


class RegionClient:
    """Fake MarketplaceClient: пишет вызовы, отдаёт BatchResult или бросает outcome."""

    marketplace = "wb"

    def __init__(self, outcome: BatchResult | Exception | None = None):
        self.outcome = outcome
        self.calls: list[tuple[tuple[int, ...], str]] = []

    async def fetch_cards_batch(self, items, dest) -> BatchResult:
        self.calls.append((tuple(items), dest))
        assert self.outcome is not None, "ответ не запланирован"
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class RecordingSender:
    """Fake NotifySender: только запись — на /region он не должен вызываться вовсе."""

    def __init__(self):
        self.sent: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> Delivered:
        self.sent.append(message)
        return Delivered()


class RegionMessageStub:
    def __init__(self):
        self.chat = SimpleNamespace(id=CHAT_ID)
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers: list[tuple[str, InlineKeyboardMarkup | None]] = []

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))


class CallbackMessageStub:
    def __init__(self):
        self.edited_markups: list[InlineKeyboardMarkup | None] = []

    async def edit_reply_markup(self, reply_markup=None):
        self.edited_markups.append(reply_markup)


class CallbackStub:
    def __init__(self, data: str | None, message: CallbackMessageStub | None = None):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answered: list[str | None] = []

    async def answer(self, text: str | None = None):
        self.answered.append(text)


def make_deps(
    store: RegionStore,
    client: RegionClient,
    sender: RecordingSender,
) -> BotDeps:
    return BotDeps(
        store=store,  # type: ignore[arg-type]  # частичный fake: только нужное /region
        client=client,  # type: ignore[arg-type]
        sender=sender,
        default_dest=KURSK,
        max_active_trackings=100,
        regions=REGIONS,
    )


def buttons_of(markup: InlineKeyboardMarkup) -> list[tuple[str, str]]:
    return [
        (b.text, b.callback_data)
        for row in markup.inline_keyboard
        for b in row
        if b.callback_data
    ]


# -------------------------------- /region --------------------------------


async def test_region_keyboard_is_built_from_config_with_current_marked():
    store = RegionStore(dest=KURSK)
    message = RegionMessageStub()
    await handle_region(message, make_deps(store, RegionClient(), RecordingSender()))

    text, markup = message.answers[0]
    assert text == messages.REGION_PROMPT_TEXT
    # справочник из config, сортировка детерминирована; текущий dest — с «✓ »
    assert buttons_of(markup) == [
        ("Белгород", f"{REGION_PREFIX}{BELGOROD}"),
        ("✓ Курск", f"{REGION_PREFIX}{KURSK}"),
        ("Москва", f"{REGION_PREFIX}{MOSCOW}"),
    ]


async def test_region_keyboard_marks_default_dest_for_unknown_user():
    store = RegionStore(dest=None)  # пользователь ещё не в БД — подсветка дефолта
    message = RegionMessageStub()
    await handle_region(message, make_deps(store, RegionClient(), RecordingSender()))

    labels = {t for t, _ in buttons_of(message.answers[0][1])}
    assert "✓ Курск" in labels
    assert "Москва" in labels and "✓ Москва" not in labels


# ------------------------- reg:<dest>: тот же dest -------------------------


async def test_region_same_dest_is_noop_toast_only():
    store = RegionStore(dest=KURSK, rows=(make_row(1, 100000),))
    client = RegionClient()
    sender = RecordingSender()
    callback = CallbackStub(f"{REGION_PREFIX}{KURSK}", CallbackMessageStub())
    await handle_region_choose(callback, make_deps(store, client, sender))

    assert callback.answered == [messages.REGION_SAME_TEXT]
    assert client.calls == []  # цены не перезапрашиваются
    assert store.region_calls == []  # set_region не вызван
    assert store.dest == KURSK
    assert callback.message.edited_markups == []
    assert sender.sent == []


# --------------------------- reg:<dest>: успех ---------------------------


async def test_region_change_fetches_user_nms_in_new_dest_and_resets_prices():
    rows = (make_row(1, 100000), make_row(2, 200000), make_row(3, 300000))
    store = RegionStore(dest=KURSK, rows=rows)
    batch = BatchResult(
        cards=(make_card(100000, 50000), make_card(200000, None)),
        missing=frozenset({300000}),
    )
    client = RegionClient(batch)
    sender = RecordingSender()
    message = CallbackMessageStub()
    callback = CallbackStub(f"{REGION_PREFIX}{MOSCOW}", message)
    await handle_region_choose(callback, make_deps(store, client, sender))

    # запрос: артикулы активного пользователя, dest — НОВЫЙ
    assert client.calls == [((100000, 200000, 300000), MOSCOW)]
    # set_region: недоступный (200000) и отсутствующий (300000) -> None
    assert store.region_calls == [
        (USER_ID, MOSCOW, {100000: 50000, 200000: None, 300000: None})
    ]
    assert store.dest == MOSCOW
    assert callback.answered == [messages.region_changed_text("Москва")]
    # клавиатура перезаписана: ✓ переехал на Москву
    assert buttons_of(message.edited_markups[0]) == [
        ("Белгород", f"{REGION_PREFIX}{BELGOROD}"),
        ("Курск", f"{REGION_PREFIX}{KURSK}"),
        ("✓ Москва", f"{REGION_PREFIX}{MOSCOW}"),
    ]


async def test_region_change_sends_no_notifications():
    rows = (make_row(1, 100000), make_row(2, 200000))
    store = RegionStore(dest=KURSK, rows=rows)
    batch = BatchResult(
        cards=(make_card(100000, 50000), make_card(200000, 90000)),
        missing=frozenset(),
    )
    sender = RecordingSender()
    callback = CallbackStub(f"{REGION_PREFIX}{MOSCOW}", CallbackMessageStub())
    await handle_region_choose(
        callback, make_deps(store, RegionClient(batch), sender)
    )

    assert sender.sent == []  # ни одного Уведомления, включая «изменилась цена»
    assert not any("изменилась цена" in m.text.lower() for m in sender.sent)


async def test_region_change_without_subscriptions_still_changes_with_empty_prices():
    store = RegionStore(dest=KURSK, rows=())
    client = RegionClient()
    callback = CallbackStub(f"{REGION_PREFIX}{MOSCOW}", CallbackMessageStub())
    await handle_region_choose(
        callback, make_deps(store, client, RecordingSender())
    )

    assert client.calls == []  # пустой список — fetch не нужен
    assert store.region_calls == [(USER_ID, MOSCOW, {})]
    assert callback.answered == [messages.region_changed_text("Москва")]


# ----------------------- reg:<dest>: ошибка WB -----------------------


async def test_region_marketplace_error_keeps_region_untouched():
    store = RegionStore(dest=KURSK, rows=(make_row(1, 100000),))
    client = RegionClient(MarketplaceError("network down"))
    sender = RecordingSender()
    message = CallbackMessageStub()
    callback = CallbackStub(f"{REGION_PREFIX}{MOSCOW}", message)
    await handle_region_choose(callback, make_deps(store, client, sender))

    assert store.region_calls == []  # set_region НЕ вызван
    assert store.dest == KURSK  # регион пользователя не изменён
    assert callback.answered == [messages.REGION_CHECK_FAILED_TEXT]
    assert message.edited_markups == []  # ✓ не переезжает
    assert sender.sent == []


# ------------------------------- мусор/codec -------------------------------


async def test_region_garbage_callback_data_answers_without_side_effects():
    store = RegionStore(dest=KURSK)
    client = RegionClient()
    callback = CallbackStub(f"{REGION_PREFIX}", CallbackMessageStub())
    await handle_region_choose(callback, make_deps(store, client, RecordingSender()))

    assert callback.answered == [None]
    assert client.calls == []
    assert store.region_calls == []


def test_region_codec_roundtrip_and_garbage():
    assert encode_region("-123") == "reg:-123"
    assert len("reg:-123".encode("utf-8")) <= 64
    assert decode_region("reg:-123") == "-123"
    assert decode_region("reg:4") == "4"
    for garbage in (None, "", "add:42", f"{REGION_PREFIX}", "region:4", "del:1"):
        assert decode_region(garbage) is None


def test_encode_region_rejects_too_long_dest():
    with pytest.raises(ValueError):
        encode_region("9" * 70)
