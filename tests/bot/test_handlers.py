# -*- coding: utf-8 -*-
"""Handler-тесты на fake-швах (spec, «Решения по тестированию» п.7).

Тестируется только поведение тонкого слоя: routing ввода, правильные вызовы
швам, тексты отказов, границы лимита/дубля, inline-подтверждение без state.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import InlineKeyboardMarkup

from app.bot import messages
from app.bot.confirm import decode_add, encode_add, handle_cancel, handle_confirm
from app.bot.handlers import BotDeps, handle_help, handle_new_product, handle_start
from app.marketplaces.base import BatchResult, CardSnapshot, MarketplaceError
from app.notify.interface import Delivered, OutgoingMessage, UserBlocked
from app.store.repository import (
    DuplicateTrackingError,
    TrackingRow,
)

USER_ID = 777
CHAT_ID = 777
DEFAULT_DEST = "4"  # Курск
NM = 1234567


# ------------------------------- фейки швов -------------------------------


class FakeStore:
    """In-memory Adapter Store: словари + запись вызовов (docs §2.2, §5.3)."""

    def __init__(
        self, *, active_count: int = 0, dest: str | None = DEFAULT_DEST, race_duplicate: bool = False
    ):
        self.users: dict[int, str | None] = {}
        if dest is not None:
            self.users[USER_ID] = dest
        self.products: dict[tuple[str, int], int] = {}
        self.subscriptions: dict[tuple[int, int], TrackingRow] = {}
        self._active_count = active_count
        self.calls: list[tuple] = []
        self.upsert_snapshots: list[CardSnapshot] = []
        self.deactivated: list[int] = []
        # имитация гонки: find_tracking «не видит», а add_tracking бросает дубль
        self.race_duplicate = race_duplicate

    # --- UI-слой ---
    async def ensure_user(self, telegram_id: int, dest: str) -> None:
        self.calls.append(("ensure_user", telegram_id, dest))
        self.users.setdefault(telegram_id, dest)

    async def get_user_dest(self, telegram_id: int) -> str | None:
        return self.users.get(telegram_id)

    async def reactivate_user(self, telegram_id: int) -> None:
        self.calls.append(("reactivate_user", telegram_id))

    async def deactivate_user(self, telegram_id: int) -> None:
        self.calls.append(("deactivate_user", telegram_id))
        self.deactivated.append(telegram_id)

    async def upsert_card(self, marketplace: str, snapshot: CardSnapshot) -> int:
        self.calls.append(("upsert_card", marketplace, snapshot.nm_id))
        key = (marketplace, snapshot.nm_id)
        if key not in self.products:
            self.products[key] = len(self.products) + 1
        self.upsert_snapshots.append(snapshot)
        return self.products[key]

    async def add_tracking(
        self, telegram_id: int, product_id: int, base_price_kop: int | None
    ) -> None:
        self.calls.append(("add_tracking", telegram_id, product_id, base_price_kop))
        if (telegram_id, product_id) in self.subscriptions or self.race_duplicate:
            raise DuplicateTrackingError("дубль")
        self.subscriptions[(telegram_id, product_id)] = TrackingRow(
            subscription_id=product_id,
            product_id=product_id,
            marketplace="wb",
            nm_id=NM,
            name=None,
            photo_url=None,
            link="",
            dest=DEFAULT_DEST,
            current_price_kop=base_price_kop,
            comparison_price_kop=base_price_kop,
            status="active",
        )
        self._active_count += 1

    async def active_tracking_count(self, telegram_id: int) -> int:
        return self._active_count

    async def find_tracking(self, telegram_id: int, product_id: int) -> TrackingRow | None:
        return self.subscriptions.get((telegram_id, product_id))

    async def remove_tracking(self, telegram_id: int, product_id: int) -> None:
        raise NotImplementedError

    async def list_trackings(self, telegram_id: int) -> tuple[TrackingRow, ...]:
        raise NotImplementedError

    async def set_region(
        self, telegram_id: int, dest: str, prices: dict[int, int | None]
    ) -> None:
        raise NotImplementedError

    # --- воркер (не используется в этом тикете) ---
    async def active_dests(self):
        raise NotImplementedError

    async def tracked_nms(self, dest):
        raise NotImplementedError

    async def apply_batch(self, dest, result, now):
        raise NotImplementedError

    async def notification_candidates(self, dest, diff):
        raise NotImplementedError

    async def unavailable_due(self, now, window):
        raise NotImplementedError

    async def commit_notification(self, subscription_id, new_comparison_kop, mark_unavailable_notified=False):
        raise NotImplementedError

    async def prune_history(self):
        raise NotImplementedError


class FakeClient:
    marketplace = "wb"

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[tuple[tuple[int, ...], str]] = []

    async def fetch_cards_batch(self, items, dest) -> BatchResult:
        self.calls.append((tuple(items), dest))
        if not self.responses:
            raise AssertionError("FakeClient: ответ не запланирован")
        outcome = self.responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeSender:
    def __init__(self, result=None):
        self.sent: list[OutgoingMessage] = []
        self.result = result or Delivered()

    async def send(self, message: OutgoingMessage):
        self.sent.append(message)
        return self.result

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


class FakeMessage:
    def __init__(self, text: str | None = None):
        self.text = text
        self.chat = SimpleNamespace(id=CHAT_ID)
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answers: list[tuple[str, InlineKeyboardMarkup | None]] = []
        self.photos: list[tuple[str, str, InlineKeyboardMarkup | None]] = []
        self.fail_photo = False

    async def answer(self, text: str, reply_markup=None):
        self.answers.append((text, reply_markup))

    async def answer_photo(self, photo: str, caption: str, reply_markup=None):
        if self.fail_photo:
            raise TelegramBadRequest(method="sendPhoto", message="photo unavailable")
        self.photos.append((photo, caption, reply_markup))

    async def edit_reply_markup(self, reply_markup=None):
        self.edited_markup = reply_markup


class FakeCallback:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answered: list[str | None] = []
        self.forbidden_on_answer = False

    async def answer(self, text: str | None = None):
        if self.forbidden_on_answer:
            raise TelegramForbiddenError(method="answerCallbackQuery", message="blocked")
        self.answered.append(text)


# ------------------------------- фикстуры -------------------------------


def make_card(
    nm_id: int = NM,
    *,
    price: int | None = 99900,
    photo: str | None = "https://card.wb.ru/photos/1.jpg",
    name: str | None = "Кроссовки мужские",
) -> CardSnapshot:
    return CardSnapshot(
        nm_id=nm_id,
        name=name,
        photo_url=photo,
        link=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        final_price_kop=price,
        available=price is not None,
    )


def batch_of(*cards: CardSnapshot, missing: tuple[int, ...] = ()) -> BatchResult:
    return BatchResult(cards=tuple(cards), missing=frozenset(missing))


def make_deps(
    store: FakeStore | None = None,
    responses: list | None = None,
    sender: FakeSender | None = None,
) -> BotDeps:
    return BotDeps(
        store=store or FakeStore(),
        client=FakeClient(responses if responses is not None else [batch_of(make_card())]),
        sender=sender or FakeSender(),
        default_dest=DEFAULT_DEST,
        max_active_trackings=100,
        regions={"Курск": DEFAULT_DEST},
    )


def add_buttons(markup: InlineKeyboardMarkup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# --------------------------------- тесты ---------------------------------


async def test_start_ensures_user_reactivates_and_greets():
    deps = make_deps(responses=[])
    message = FakeMessage("/start")
    await handle_start(message, deps)
    assert ("ensure_user", USER_ID, DEFAULT_DEST) in deps.store.calls
    assert ("reactivate_user", USER_ID) in deps.store.calls
    assert deps.sender.texts == [messages.START_TEXT]
    assert deps.sender.sent[0].chat_id == CHAT_ID


async def test_help_sends_help_text():
    deps = make_deps(responses=[])
    await handle_help(FakeMessage("/help"), deps)
    assert deps.sender.texts == [messages.HELP_TEXT]


async def test_link_routes_to_preview_without_state():
    card = make_card()
    deps = make_deps(responses=[batch_of(card)])
    message = FakeMessage("https://www.wildberries.ru/catalog/1234567/detail.aspx")
    await handle_new_product(message, deps)
    assert deps.client.calls == [((NM,), DEFAULT_DEST)]
    assert len(message.photos) == 1
    photo, caption, markup = message.photos[0]
    assert photo == card.photo_url
    assert "999" in caption and "Кроссовки" in caption
    assert add_buttons(markup) == [encode_add(NM), "cancel"]
    assert ("add_tracking", USER_ID, 1, 99900) not in deps.store.calls


async def test_bare_sku_routes_to_preview():
    deps = make_deps(responses=[batch_of(make_card())])
    message = FakeMessage("1234567")
    await handle_new_product(message, deps)
    assert deps.client.calls == [((NM,), DEFAULT_DEST)]
    assert message.photos or message.answers


async def test_missing_product_says_not_found():
    deps = make_deps(responses=[batch_of(missing=(NM,))])
    message = FakeMessage("1234567")
    await handle_new_product(message, deps)
    assert deps.sender.texts == [messages.PRODUCT_NOT_FOUND_TEXT]
    assert not message.photos and not message.answers


async def test_unavailable_product_still_shown_with_note():
    card = make_card(price=None)
    deps = make_deps(responses=[batch_of(card)])
    message = FakeMessage("1234567")
    await handle_new_product(message, deps)
    assert not deps.sender.sent
    caption = message.photos[0][1]
    assert messages.UNAVAILABLE_NOTE in caption


async def test_photo_failure_falls_back_to_text():
    deps = make_deps(responses=[batch_of(make_card())])
    message = FakeMessage("1234567")
    message.fail_photo = True
    await handle_new_product(message, deps)
    assert not message.photos
    text, markup = message.answers[0]
    assert "999" in text
    assert add_buttons(markup) == [encode_add(NM), "cancel"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("https://go.wb.ru/AbCd", messages.SHORT_LINK_HINT),
        ("артикулы 123456 и 234567", messages.AMBIGUOUS_HINT),
        ("привет, как дела?", messages.PARSE_NOT_FOUND_HINT),
    ],
)
async def test_parse_hints_do_not_reach_marketplace(text, expected):
    deps = make_deps(responses=[])
    await handle_new_product(FakeMessage(text), deps)
    assert deps.sender.texts == [expected]
    assert deps.client.calls == []


async def test_marketplace_error_says_try_later():
    deps = make_deps(responses=[MarketplaceError("5xx")])
    await handle_new_product(FakeMessage("1234567"), deps)
    assert deps.sender.texts == [messages.WB_ERROR_TEXT]


async def test_limit_below_offers_preview_at_limit_refuses():
    # 99 активных — лимит 100: место есть, показываем предпросмотр.
    deps = make_deps(store=FakeStore(active_count=99), responses=[batch_of(make_card())])
    message = FakeMessage("1234567")
    await handle_new_product(message, deps)
    assert message.photos and not deps.sender.sent

    # 100 активных: добавление 101-го невозможно — вежливый отказ без предпросмотра.
    deps2 = make_deps(store=FakeStore(active_count=100), responses=[batch_of(make_card())])
    message2 = FakeMessage("1234567")
    await handle_new_product(message2, deps2)
    assert not message2.photos and not message2.answers
    assert deps2.sender.texts == [messages.limit_reached_text(100)]
    assert deps2.store.upsert_snapshots  # карточка обновлена — это допустимо
    assert not any(c[0] == "add_tracking" for c in deps2.store.calls)


async def test_duplicate_offers_hint_without_preview():
    store = FakeStore()
    deps = make_deps(
        store=store,
        responses=[batch_of(make_card())] * 3,  # превью -> подтверждение -> повтор
    )
    await handle_new_product(FakeMessage("1234567"), deps)
    callback = FakeCallback(encode_add(NM), FakeMessage())
    await handle_confirm(callback, deps)
    assert any(c[0] == "add_tracking" for c in store.calls)

    # Повтор — подсказка, предпросмотра и add_tracking нет.
    deps.sender.sent.clear()
    message = FakeMessage("1234567")
    await handle_new_product(message, deps)
    assert deps.sender.texts == [messages.ALREADY_TRACKING_TEXT]
    assert not message.photos and not message.answers


async def test_confirm_refetches_and_adds_tracking_with_base_price():
    card = make_card(price=99900)
    store = FakeStore()
    deps = make_deps(store=store, responses=[batch_of(card), batch_of(card)])
    message = FakeMessage("1234567")
    await handle_new_product(message, deps)
    product_id = store.products[("wb", NM)]

    callback_message = FakeMessage()
    callback = FakeCallback(encode_add(NM), callback_message)
    await handle_confirm(callback, deps)

    assert deps.client.calls[-1] == ((NM,), DEFAULT_DEST)
    assert ("add_tracking", USER_ID, product_id, 99900) in store.calls
    assert deps.sender.texts == [messages.added_text(card)]
    assert callback_message.edited_markup is None  # кнопки погашены
    assert callback.answered == [None]


async def test_confirm_uses_fresh_price_not_preview_price():
    stale = make_card(price=99900)
    fresh = make_card(price=85000)
    store = FakeStore()
    deps = make_deps(store=store, responses=[batch_of(stale), batch_of(fresh)])
    await handle_new_product(FakeMessage("1234567"), deps)
    await handle_confirm(FakeCallback(encode_add(NM), FakeMessage()), deps)
    product_id = store.products[("wb", NM)]
    assert ("add_tracking", USER_ID, product_id, 85000) in store.calls
    assert messages.added_text(fresh) in deps.sender.texts


async def test_confirm_unavailable_product_stores_none_base():
    card = make_card(price=None)
    store = FakeStore()
    deps = make_deps(store=store, responses=[batch_of(card), batch_of(card)])
    await handle_new_product(FakeMessage("1234567"), deps)
    await handle_confirm(FakeCallback(encode_add(NM), FakeMessage()), deps)
    product_id = store.products[("wb", NM)]
    assert ("add_tracking", USER_ID, product_id, None) in store.calls


async def test_confirm_race_duplicate_reports_hint():
    # Гонка: find_tracking не видит, add_tracking бросает DuplicateTrackingError.
    store = FakeStore(race_duplicate=True)
    deps = make_deps(store=store, responses=[batch_of(make_card())])
    await handle_confirm(FakeCallback(encode_add(NM), FakeMessage()), deps)
    assert deps.sender.texts == [messages.ALREADY_TRACKING_TEXT]
    assert deps.client.calls[-1] == ((NM,), DEFAULT_DEST)
    assert store.subscriptions == {}  # отслеживание не создано


async def test_confirm_second_fetch_error_says_try_later():
    deps = make_deps(responses=[MarketplaceError("timeout")])
    await handle_confirm(FakeCallback(encode_add(NM), FakeMessage()), deps)
    assert deps.sender.texts == [messages.WB_ERROR_TEXT]


async def test_confirm_missing_says_not_found():
    deps = make_deps(responses=[batch_of(make_card()), batch_of(missing=(NM,))])
    await handle_new_product(FakeMessage("1234567"), deps)
    deps.sender.sent.clear()
    await handle_confirm(FakeCallback(encode_add(NM), FakeMessage()), deps)
    assert deps.sender.texts == [messages.PRODUCT_NOT_FOUND_TEXT]


async def test_confirm_forbidden_on_answer_swallowed_and_delivered_via_results():
    store = FakeStore()
    deps = make_deps(store=store, responses=[batch_of(make_card()), batch_of(make_card())])
    callback = FakeCallback(encode_add(NM), FakeMessage())
    callback.forbidden_on_answer = True
    await handle_confirm(callback, deps)  # не должно бросить
    assert any(c[0] == "add_tracking" for c in store.calls)
    assert deps.sender.texts == [messages.added_text(make_card())]


async def test_cancel_creates_nothing_and_dismisses_keyboard():
    store = FakeStore()
    deps = make_deps(store=store, responses=[batch_of(make_card())])
    message = FakeMessage()
    callback = FakeCallback("cancel", message)
    await handle_cancel(callback, deps)
    assert not any(c[0] == "add_tracking" for c in store.calls)
    assert deps.sender.sent == []  # только тост, без сообщения
    assert callback.answered == [messages.CANCEL_TOAST]
    assert message.edited_markup is None


async def test_user_blocked_signal_deactivates_user():
    store = FakeStore()
    sender = FakeSender(result=UserBlocked())
    deps = BotDeps(
        store=store,
        client=FakeClient([batch_of(make_card()), batch_of(make_card())]),
        sender=sender,
        default_dest=DEFAULT_DEST,
        max_active_trackings=100,
        regions={"Курск": DEFAULT_DEST},
    )
    await handle_new_product(FakeMessage("1234567"), deps)
    assert store.deactivated == []  # предпросмотр идёт мимо NotifySender
    await handle_confirm(FakeCallback(encode_add(NM), FakeMessage()), deps)
    assert store.deactivated == [USER_ID]


def test_callback_codec_roundtrip_and_garbage():
    assert encode_add(NM) == f"add:{NM}"
    assert decode_add(encode_add(NM)) == NM
    for garbage in (None, "", "cancel", "add:", "add:abc", "add:1b", "del:123"):
        assert decode_add(garbage) is None
