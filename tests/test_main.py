import asyncio
import logging

import pytest

from app.config import Settings
from app.main import run_app


@pytest.fixture
def settings():
    return Settings(
        bot_token="1:stub",
        default_dest="Курск",
        database_path=":memory:",
        _env_file=None,
    )


class FakeStore:
    """Воркер дойдёт до store только после сна 45–75 мин — в тестах не вызывается."""


class FakeClient:
    marketplace = "wb"


class FakeSender:
    async def send(self, message):
        raise AssertionError("в этих тестах ничего не отправляется")


def make_fake_start_bot(started: asyncio.Event, cancelled: asyncio.Event):
    async def _start(dispatcher):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    return _start


async def test_run_app_starts_both_tasks_and_shuts_down_cleanly(settings, caplog):
    caplog.set_level(logging.INFO)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop = asyncio.Event()
    runner = asyncio.create_task(
        run_app(
            settings,
            stop,
            store=FakeStore(),
            client=FakeClient(),
            sender=FakeSender(),
            bot=object(),
            start_bot=make_fake_start_bot(started, cancelled),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    # воркер-задача живёт и исполняет run_check_cycle_loop (первое действие — сон)
    assert "Цикл проверки: сон" in caplog.text

    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert runner.done() and not runner.cancelled()
    assert "shutdown requested, cancelling tasks" in caplog.text
    assert "shutdown complete" in caplog.text
    # polling-задача действительно была отменена при остановке
    assert cancelled.is_set()
