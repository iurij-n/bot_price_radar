"""Тесты Adapter'а TelegramNotifySender на fake-боте и фейковых часах.

Поверхность тестов — Interface NotifySender (§2.4 docs/architecture.md):
результаты вместо исключений, троттлинг ≥1 msg/s, FloodWait-ретраи, Forbidden.
"""

import asyncio

from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
)

from app.notify.interface import Delivered, Failed, OutgoingMessage, UserBlocked
from app.notify.telegram import TelegramNotifySender


class FakeClock:
    """Инъектируемые monotonic-часы: sleep сдвигает виртуальное время, не реальное."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # как реальный сон: уступить loop; параллельные sleep одного
        # виртуального времени не сдвигают часы дважды
        target = self.now + seconds
        await asyncio.sleep(0)
        self.now = max(self.now, target)


class FakeBot:
    """Фейковый aiogram Bot: пишет вызовы sendMessage, инжектит исключения по скрипту."""

    def __init__(self, exceptions=(), clock: FakeClock | None = None) -> None:
        self.calls: list[dict] = []
        self.call_times: list[float] = []
        self._exceptions = list(exceptions)
        self._clock = clock

    async def send_message(self, **kwargs) -> None:
        self.calls.append(kwargs)
        if self._clock is not None:
            self.call_times.append(self._clock())
        if self._exceptions:
            raise self._exceptions.pop(0)


def make_sender(bot: FakeBot, clock: FakeClock) -> TelegramNotifySender:
    return TelegramNotifySender(bot, clock=clock, sleep=clock.sleep)


async def test_send_with_button_returns_delivered_and_passes_keyboard():
    clock = FakeClock()
    bot = FakeBot(clock=clock)
    sender = make_sender(bot, clock)

    result = await sender.send(
        OutgoingMessage(chat_id=42, text="Цена упала", button=("Открыть товар", "https://wb.ru/1"))
    )

    assert isinstance(result, Delivered)
    assert len(bot.calls) == 1
    call = bot.calls[0]
    assert call["chat_id"] == 42
    assert call["text"] == "Цена упала"
    markup = call["reply_markup"]
    button = markup.inline_keyboard[0][0]
    assert button.text == "Открыть товар"
    assert button.url == "https://wb.ru/1"


async def test_send_with_callback_button_adds_second_row():
    clock = FakeClock()
    bot = FakeBot(clock=clock)
    sender = make_sender(bot, clock)

    result = await sender.send(
        OutgoingMessage(
            chat_id=42,
            text="строка /list",
            button=("Открыть товар", "https://wb.ru/1"),
            callback=("🗑 Удалить", "del:7"),
        )
    )

    assert isinstance(result, Delivered)
    keyboard = bot.calls[0]["reply_markup"].inline_keyboard
    assert len(keyboard) == 2
    assert keyboard[0][0].url == "https://wb.ru/1"
    assert keyboard[0][0].callback_data is None
    assert keyboard[1][0].text == "🗑 Удалить"
    assert keyboard[1][0].callback_data == "del:7"
    assert keyboard[1][0].url is None


async def test_send_with_callback_only_renders_single_row():
    clock = FakeClock()
    bot = FakeBot(clock=clock)
    sender = make_sender(bot, clock)

    await sender.send(
        OutgoingMessage(chat_id=42, text="без ссылки", button=None, callback=("🗑", "del:1"))
    )

    keyboard = bot.calls[0]["reply_markup"].inline_keyboard
    assert len(keyboard) == 1
    assert keyboard[0][0].callback_data == "del:1"


async def test_forbidden_returns_userblocked_and_never_raises():
    clock = FakeClock()
    bot = FakeBot(exceptions=[TelegramForbiddenError(method=None, message="Forbidden")], clock=clock)
    sender = make_sender(bot, clock)

    result = await sender.send(OutgoingMessage(chat_id=7, text="Уведомление", button=None))

    assert isinstance(result, UserBlocked)
    assert len(bot.calls) == 1  # сигнал, не действие: ретраев и деактивации внутри шва нет


async def test_persistent_floodwait_fails_after_three_attempts_without_hang():
    clock = FakeClock()
    bot = FakeBot(
        exceptions=[TelegramRetryAfter(method=None, message="Flood", retry_after=1) for _ in range(5)],
        clock=clock,
    )
    sender = make_sender(bot, clock)

    result = await asyncio.wait_for(
        sender.send(OutgoingMessage(chat_id=7, text="Уведомление", button=None)),
        timeout=2,
    )

    assert isinstance(result, Failed)
    assert result.attempt_count == 3
    assert len(bot.calls) == 3
    assert "TelegramRetryAfter" in result.exc_summary


async def test_floodwait_retries_respect_retry_after_then_deliver():
    clock = FakeClock()
    bot = FakeBot(
        exceptions=[
            TelegramRetryAfter(method=None, message="Flood", retry_after=1),
            TelegramRetryAfter(method=None, message="Flood", retry_after=2),
        ],
        clock=clock,
    )
    sender = make_sender(bot, clock)

    result = await asyncio.wait_for(
        sender.send(OutgoingMessage(chat_id=7, text="Уведомление", button=None)),
        timeout=2,
    )

    assert isinstance(result, Delivered)
    assert len(bot.calls) == 3  # 2 FloodWait + успешная попытка
    # виртуальное время между попытками >= retry_after (1с, затем 2с)
    assert bot.call_times[1] - bot.call_times[0] >= 1
    assert bot.call_times[2] - bot.call_times[1] >= 2


async def test_ten_sends_take_at_least_nine_seconds_of_fake_time():
    clock = FakeClock()
    bot = FakeBot(clock=clock)
    sender = make_sender(bot, clock)

    results = [
        await sender.send(OutgoingMessage(chat_id=1, text=f"msg {i}", button=None))
        for i in range(10)
    ]

    assert all(isinstance(r, Delivered) for r in results)
    assert len(bot.calls) == 10
    assert clock.now >= 9.0  # 9 меж-отправочных интервалов по ≥1с
    gaps = [t2 - t1 for t1, t2 in zip(bot.call_times, bot.call_times[1:])]
    assert all(gap >= 1.0 for gap in gaps)


async def test_other_api_error_returns_failed_without_raising():
    clock = FakeClock()
    bot = FakeBot(exceptions=[TelegramNetworkError(method=None, message="net down")], clock=clock)
    sender = make_sender(bot, clock)

    result = await sender.send(OutgoingMessage(chat_id=7, text="Уведомление", button=None))

    assert isinstance(result, Failed)
    assert result.attempt_count == 1
    assert "TelegramNetworkError" in result.exc_summary


async def test_unexpected_exception_returns_failed_without_raising():
    clock = FakeClock()
    bot = FakeBot(exceptions=[RuntimeError("bot broken")], clock=clock)
    sender = make_sender(bot, clock)

    result = await sender.send(OutgoingMessage(chat_id=7, text="Уведомление", button=None))

    assert isinstance(result, Failed)
    assert "RuntimeError" in result.exc_summary


async def test_concurrent_sends_are_serialized_by_throttle():
    clock = FakeClock()
    bot = FakeBot(clock=clock)
    sender = make_sender(bot, clock)

    messages = [OutgoingMessage(chat_id=i, text=f"msg {i}", button=None) for i in range(10)]
    results = await asyncio.wait_for(
        asyncio.gather(*(sender.send(m) for m in messages)),
        timeout=5,
    )

    assert all(isinstance(r, Delivered) for r in results)
    assert len(bot.calls) == 10
    # без реального сна: всё время — виртуальное, и оно >= 9с на 10 сообщений
    assert clock.now >= 9.0
    gaps = [t2 - t1 for t1, t2 in zip(bot.call_times, bot.call_times[1:])]
    assert all(gap >= 1.0 for gap in gaps)
