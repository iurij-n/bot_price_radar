# -*- coding: utf-8 -*-
"""Handler-тесты /list и 🗑-удаления на fake-швах (тикет 06).

Тонкий слой: правильное число и порядок send через NotifySender, тексты
строк, кнопка-колбэк del:<product_id>, идемпотентность удаления, реакция на
UserBlocked/Failed. Prune истории пары — ответственность Store, покрыта
tests/store/test_repository.py::test_remove_tracking_prunes_history_only_at_zero
и соседями; здесь не дублируем.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramBadRequest

from app.bot import messages
from app.bot.handlers import BotDeps, handle_delete, handle_list
from app.bot.keyboards import DEL_LABEL, OPEN_LINK_LABEL, decode_del, encode_del
from app.notify.interface import Delivered, DeliveryResult, Failed, OutgoingMessage, UserBlocked
from app.store.repository import TrackingRow

USER_ID = 555
CHAT_ID = 555
DEST = "4"  # Курск


# ------------------------------- фейки швов -------------------------------


def make_row(
    subscription_id: int,
    product_id: int,
    nm_id: int,
    *,
    price: int | None,
    name: str | None = "Товар",
) -> TrackingRow:
    return TrackingRow(
        subscription_id=subscription_id,
        product_id=product_id,
        marketplace="wb",
        nm_id=nm_id,
        name=name,
        photo_url=None,
        link=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        dest=DEST,
        current_price_kop=price,
        comparison_price_kop=price,
        status="active",
    )


class ListStore:
    """Dict-based fake Store (тот же Interface): Otслеживания, удаление, dest."""

    def __init__(self, rows: tuple[TrackingRow, ...] = ()):
        self.subscriptions: dict[tuple[int, int], TrackingRow] = {
            (USER_ID, r.product_id): r for r in rows
        }
        self.removed: list[tuple[int, int]] = []
        self.deactivated: list[int] = []

    async def list_trackings(self, telegram_id: int) -> tuple[TrackingRow, ...]:
        rows = [r for (tid, _), r in self.subscriptions.items() if tid == telegram_id]
        return tuple(sorted(rows, key=lambda r: r.subscription_id))

    async def remove_tracking(self, telegram_id: int, product_id: int) -> None:
        # как SqlStore: неизвестная пара — молчаливой no-op (идемпотентность)
        self.removed.append((telegram_id, product_id))
        self.subscriptions.pop((telegram_id, product_id), None)

    async def get_user_dest(self, telegram_id: int) -> str | None:
        return DEST

    async def deactivate_user(self, telegram_id: int) -> None:
        self.deactivated.append(telegram_id)


class ScriptSender:
    """Fake NotifySender: пишет OutgoingMessage, скрипт результатов по одному."""

    def __init__(self, results: list[DeliveryResult] | None = None):
        self.sent: list[OutgoingMessage] = []
        self._results = list(results or [])

    async def send(self, message: OutgoingMessage) -> DeliveryResult:
        self.sent.append(message)
        return self._results.pop(0) if self._results else Delivered()

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]


class MessageStub:
    def __init__(self):
        self.chat = SimpleNamespace(id=CHAT_ID)
        self.from_user = SimpleNamespace(id=USER_ID)


class CallbackMessageStub:
    def __init__(self, *, fail_edit: bool = False):
        self.edited_texts: list[str] = []
        self.edited_markups: list[object] = []
        self.fail_edit = fail_edit

    async def edit_text(self, text: str, *args, **kwargs):
        if self.fail_edit:
            raise TelegramBadRequest(method="editMessageText", message="message is not modified")
        self.edited_texts.append(text)

    async def edit_reply_markup(self, reply_markup=None):
        if self.fail_edit:
            raise TelegramBadRequest(method="editMessageReplyMarkup", message="no markup")
        self.edited_markups.append(reply_markup)


class CallbackStub:
    def __init__(self, data: str | None, message: CallbackMessageStub | None = None):
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=USER_ID)
        self.answered: list[str | None] = []

    async def answer(self, text: str | None = None):
        self.answered.append(text)


def make_deps(store: ListStore, sender: ScriptSender) -> BotDeps:
    return BotDeps(
        store=store,  # type: ignore[arg-type]  # частичный fake: только нужное /list и del
        client=None,  # type: ignore[arg-type]  # эти handlers маркетплейс не трогают
        sender=sender,
        default_dest=DEST,
        max_active_trackings=100,
        regions={"Курск": DEST},
    )


# --------------------------------- /list ---------------------------------


async def test_list_sends_one_message_per_tracking_sorted_by_subscription():
    rows = (
        make_row(3, 30, 300000, price=1234500, name="Платье"),
        make_row(1, 10, 100000, price=10050, name="Носки"),
    )
    store = ListStore(rows)
    sender = ScriptSender()
    await handle_list(MessageStub(), make_deps(store, sender))

    assert len(sender.sent) == 2
    first, second = sender.sent
    assert first.text.startswith("Носки") and second.text.startswith("Платье")
    for msg in (first, second):
        assert msg.chat_id == CHAT_ID
    assert f"Артикул: {100000}" in first.text
    # копейки -> рубли: 1 234 500 копеек = 12 345 ₽
    assert f"Цена: {messages.format_price_kop(1234500)}" in second.text
    assert second.text.count("12\u00a0345\u00a0₽") == 1


async def test_list_row_has_open_url_and_delete_callback_buttons():
    store = ListStore((make_row(1, 10, 100000, price=100),))
    sender = ScriptSender()
    await handle_list(MessageStub(), make_deps(store, sender))

    msg = sender.sent[0]
    assert msg.button == (OPEN_LINK_LABEL, "https://www.wildberries.ru/catalog/100000/detail.aspx")
    assert msg.callback == (DEL_LABEL, encode_del(10)) == (DEL_LABEL, "del:10")
    assert len("del:10".encode("utf-8")) <= 64


async def test_list_without_price_says_no_price_single_text():
    # current_price_kop=None покрывает оба случая (нет строки цен / недоступен):
    # TrackingRow их не различает — один текст «нет цены в вашем регионе».
    store = ListStore((make_row(1, 10, 100000, price=None),))
    sender = ScriptSender()
    await handle_list(MessageStub(), make_deps(store, sender))

    assert messages.UNAVAILABLE_NOTE in sender.texts[0]
    assert "Цена:" not in sender.texts[0]


async def test_list_empty_sends_hint():
    sender = ScriptSender()
    await handle_list(MessageStub(), make_deps(ListStore(), sender))
    assert sender.texts == [messages.LIST_EMPTY_TEXT]


async def test_list_user_blocked_deactivates_once_and_stops_delivery():
    store = ListStore(
        (
            make_row(1, 10, 100000, price=100),
            make_row(2, 20, 200000, price=200),
            make_row(3, 30, 300000, price=300),
        )
    )
    sender = ScriptSender([Delivered(), UserBlocked(), Delivered()])
    await handle_list(MessageStub(), make_deps(store, sender))

    assert len(sender.sent) == 2  # после блока выдача остановлена
    assert store.deactivated == [USER_ID]  # ровно одна деактивация


async def test_list_failed_is_logged_and_does_not_stop_delivery():
    store = ListStore(
        (make_row(1, 10, 100000, price=100), make_row(2, 20, 200000, price=200))
    )
    sender = ScriptSender([Failed(exc_summary="NetworkError", attempt_count=3), Delivered()])
    await handle_list(MessageStub(), make_deps(store, sender))

    assert len(sender.sent) == 2
    assert store.deactivated == []


# -------------------------------- удаление --------------------------------


async def test_delete_removes_tracking_and_restyles_message():
    store = ListStore((make_row(1, 10, 100000, price=100),))
    message = CallbackMessageStub()
    callback = CallbackStub("del:10", message)
    await handle_delete(callback, make_deps(store, ScriptSender()))

    assert store.removed == [(USER_ID, 10)]
    assert await store.list_trackings(USER_ID) == ()  # повторный /list без товара
    assert message.edited_texts == [messages.DELETED_TEXT]
    assert message.edited_markups == [None]
    assert callback.answered == [None]


async def test_delete_unknown_pair_still_reports_deleted_idempotently():
    store = ListStore()
    message = CallbackMessageStub()
    callback = CallbackStub("del:999", message)
    await handle_delete(callback, make_deps(store, ScriptSender()))

    assert store.removed == [(USER_ID, 999)]
    assert message.edited_texts == [messages.DELETED_TEXT]


async def test_delete_edit_errors_are_swallowed():
    store = ListStore((make_row(1, 10, 100000, price=100),))
    message = CallbackMessageStub(fail_edit=True)
    callback = CallbackStub("del:10", message)
    await handle_delete(callback, make_deps(store, ScriptSender()))  # не бросает

    assert store.removed == [(USER_ID, 10)]  # удаление всё равно состоялось


async def test_delete_without_message_still_removes():
    store = ListStore((make_row(1, 10, 100000, price=100),))
    callback = CallbackStub("del:10", None)
    await handle_delete(callback, make_deps(store, ScriptSender()))
    assert store.removed == [(USER_ID, 10)]


async def test_delete_garbage_callback_data_does_nothing_but_answer():
    store = ListStore()
    message = CallbackMessageStub()
    callback = CallbackStub("del:abc", message)
    await handle_delete(callback, make_deps(store, ScriptSender()))

    assert store.removed == []
    assert message.edited_texts == []
    assert callback.answered == [None]


async def test_list_then_delete_then_list_is_empty():
    store = ListStore((make_row(1, 10, 100000, price=100),))
    deps = make_deps(store, ScriptSender())
    await handle_list(MessageStub(), deps)
    await handle_delete(CallbackStub("del:10", CallbackMessageStub()), deps)

    sender2 = ScriptSender()
    await handle_list(MessageStub(), make_deps(store, sender2))
    assert sender2.texts == [messages.LIST_EMPTY_TEXT]


# --------------------------------- codec ---------------------------------


def test_del_codec_roundtrip_and_garbage():
    assert encode_del(42) == "del:42"
    assert decode_del("del:42") == 42
    for garbage in (None, "", "add:42", "del:", "del:1b", "del:-1"):
        assert decode_del(garbage) is None


def test_encode_del_rejects_too_long_payload():
    with pytest.raises(ValueError):
        encode_del(10**60)
