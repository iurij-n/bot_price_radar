"""Композиция main.py: один NotifySender на обе задачи, реальные Adapter'ы
по умолчанию, закрытие http-клиента/engine/Bot при остановке, отсутствие сети в тестах.
"""

import asyncio
import logging

import pytest
from aiogram import Bot, Dispatcher
from sqlalchemy.ext.asyncio import AsyncEngine

import app.main as main_mod
from app.bot.handlers import BotDeps
from app.config import Settings
from app.main import run_app
from app.marketplaces.http_client import CurlCffiClient
from app.marketplaces.wb import WBClient
from app.notify.telegram import TelegramNotifySender
from app.store.repository import SqlStore


@pytest.fixture
def settings():
    return Settings(
        bot_token="1:stub",
        default_dest="Курск",
        database_path=":memory:",
        _env_file=None,
    )


class FakeStore:
    pass


class FakeClient:
    marketplace = "wb"


class FakeSender:
    async def send(self, message):
        raise AssertionError("в этих тестах ничего не отправляется")


def make_fake_start_bot(holder: dict, started: asyncio.Event, cancelled: asyncio.Event):
    async def _start(dispatcher):
        holder["dispatcher"] = dispatcher
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    return _start


def deps_from(dispatcher: Dispatcher) -> BotDeps:
    """Достаёт BotDeps из замыкания handler'ов — доказательство, что polling
    видит именно те экземпляр(ы), что и воркер."""
    router = dispatcher.sub_routers[0]
    for obs in (router.message, router.callback_query):
        for handler in obs.handlers:
            for cell in handler.callback.__closure__ or ():
                contents = cell.cell_contents
                if isinstance(contents, BotDeps):
                    return contents
    raise AssertionError("BotDeps не найден в замыканиях handler'ов")


async def test_one_sender_instance_shared_by_polling_and_worker(
    settings, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    created_senders: list = []
    real_sender_cls = main_mod.TelegramNotifySender

    def spy_sender(bot):
        instance = real_sender_cls(bot)
        created_senders.append(instance)
        return instance

    monkeypatch.setattr(main_mod, "TelegramNotifySender", spy_sender)

    holder: dict = {}
    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop = asyncio.Event()
    store, client = FakeStore(), FakeClient()
    runner = asyncio.create_task(
        run_app(
            settings,
            stop,
            store=store,
            client=client,
            bot=object(),
            start_bot=make_fake_start_bot(holder, started, cancelled),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    assert "Цикл проверки: сон" in caplog.text  # воркер крутится с тем же sender-местом

    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert len(created_senders) == 1
    sender = created_senders[0]
    deps = deps_from(holder["dispatcher"])
    assert deps.sender is sender  # экземпляр NotifySender один в точке входа (§2.4)
    assert deps.store is store
    assert deps.client is client
    assert deps.default_dest == settings.default_dest
    assert deps.max_active_trackings == settings.max_active_trackings


async def test_default_composition_builds_real_adapters_and_disposes_resources(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "data" / "price_radar.db"
    settings = Settings(
        bot_token="1:test",
        default_dest="4",
        database_path=str(db_path),
        _env_file=None,
    )

    disposed: list = []
    real_dispose = AsyncEngine.dispose

    async def spy_dispose(self, close=True):
        disposed.append(self)
        return await real_dispose(self, close=close)

    monkeypatch.setattr(AsyncEngine, "dispose", spy_dispose)

    created_clients: list = []
    real_client_cls = CurlCffiClient

    class SpyClient(real_client_cls):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._closed = False
            created_clients.append(self)

        async def aclose(self):
            self._closed = True
            await super().aclose()

    monkeypatch.setattr(main_mod, "CurlCffiClient", SpyClient)

    created_senders: list = []
    real_sender_cls = main_mod.TelegramNotifySender

    def spy_sender(bot):
        instance = real_sender_cls(bot)
        created_senders.append(instance)
        return instance

    monkeypatch.setattr(main_mod, "TelegramNotifySender", spy_sender)

    holder: dict = {}
    started = asyncio.Event()
    cancelled = asyncio.Event()
    stop = asyncio.Event()
    runner = asyncio.create_task(
        run_app(settings, stop, start_bot=make_fake_start_bot(holder, started, cancelled))
    )
    await asyncio.wait_for(started.wait(), timeout=5)

    deps = deps_from(holder["dispatcher"])
    assert isinstance(deps.store, SqlStore)
    assert isinstance(deps.client, WBClient)
    assert isinstance(deps.sender, TelegramNotifySender)
    assert deps.sender is created_senders[0]
    assert len(created_senders) == 1
    # каталог БД создан, схема инициализирована (init_db открывает файл)
    assert db_path.parent.is_dir()
    assert db_path.exists()

    stop.set()
    await asyncio.wait_for(runner, timeout=5)

    assert cancelled.is_set()
    assert len(created_clients) == 1
    assert created_clients[0]._closed
    assert len(disposed) == 1


async def test_injected_overrides_build_no_real_adapters(settings, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("при инъекции зависимостей реальные Adapter'ы не строятся")

    monkeypatch.setattr(main_mod, "make_engine", boom)
    monkeypatch.setattr(main_mod, "TelegramNotifySender", boom)
    monkeypatch.setattr(main_mod, "Bot", boom)
    monkeypatch.setattr(main_mod, "CurlCffiClient", boom)

    holder: dict = {}
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
            start_bot=make_fake_start_bot(holder, started, cancelled),
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    stop.set()
    await asyncio.wait_for(runner, timeout=5)
    assert cancelled.is_set()


async def test_start_bot_polling_delegates_to_aiogram_without_signals(monkeypatch):
    calls: dict = {}

    async def fake_start_polling(self, *bots, handle_signals=True, **kwargs):
        calls["bots"] = bots
        calls["handle_signals"] = handle_signals

    monkeypatch.setattr(Dispatcher, "start_polling", fake_start_polling)
    dispatcher = Dispatcher()
    bot = Bot(token="1:stub")
    await main_mod.start_bot_polling(dispatcher, bot)
    assert calls["bots"] == (bot,)
    assert calls["handle_signals"] is False
