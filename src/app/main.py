"""Композиция приложения: сборка Adapter'ов и две asyncio-задачи в одном процессе (ADR-0002).

main.py — единственное место, где конкретные Adapter'ы (SqlStore, WBClient,
TelegramNotifySender, aiogram Dispatcher) встречаются (docs/architecture.md §1,
§2.4): один экземпляр NotifySender разделяют polling-бот и воркер Цикла
проверки; один httpx.AsyncClient принадлежит композиции и закрывается при
остановке; engine гасится через ``engine.dispose()``.

``run_app(settings, stop)`` принимает опциональные overrides
store/client/sender/bot/start_bot — тесты собирают приложение без сети;
``main()`` из тестов не вызывается.
"""

import asyncio
import contextlib
import functools
import logging
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from aiogram import Bot, Dispatcher, Router

from app.bot.handlers import BotDeps, register_handlers
from app.config import Settings, load_settings
from app.marketplaces.base import MarketplaceClient
from app.marketplaces.http_client import CurlCffiClient
from app.marketplaces.wb import WBClient, _DEFAULT_TIMEOUT
from app.notify.interface import NotifySender
from app.notify.telegram import TelegramNotifySender
from app.store.db import init_db, make_engine, make_session_factory
from app.store.repository import SqlStore, Store
from app.workers.cycle import run_check_cycle_loop

logger = logging.getLogger("app.main")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def start_bot_polling(dispatcher: Dispatcher, bot: Bot) -> None:
    """Long polling aiogram.

    ``handle_signals=False``: сигналы SIGINT/SIGTERM принадлежат stop-событию
    ``run_app`` (иначе aiogram перетёр бы обработчики и обходился без него).
    ``handle_reexceptions`` — по умолчанию (ErrorHandlers).
    """
    await dispatcher.start_polling(bot, handle_signals=False)


async def run_app(
    settings: Settings,
    stop: asyncio.Event | None = None,
    *,
    store: Store | None = None,
    client: MarketplaceClient | None = None,
    sender: NotifySender | None = None,
    bot: Bot | None = None,
    start_bot: Callable[[Dispatcher], Awaitable[None]] | None = None,
) -> None:
    """Собирает композицию, запускает polling и Цикл проверки, корректно гасит по stop.

    Ресурсы, созданные здесь же (engine, CurlCffiClient, Bot), закрываются
    в finally; инъектированные зависимости не трогаем — ими владеет caller.
    """
    engine = None
    http_client: CurlCffiClient | None = None
    bot_created = False

    if store is None:
        if settings.database_path != ":memory:":
            Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
        engine = make_engine(settings.database_path)
        await init_db(engine)
        store = SqlStore(make_session_factory(engine), marketplace="wb")

    if client is None:
        http_client = CurlCffiClient(timeout=_DEFAULT_TIMEOUT)
        client = WBClient(http_client)

    if bot is None:
        bot = Bot(token=settings.bot_token.get_secret_value())
        bot_created = True
    if sender is None:
        # Единственный экземпляр NotifySender на процесс (§2.4): его получают
        # и BotDeps handler'ов, и воркер ниже — троттлинг не разъедется.
        sender = TelegramNotifySender(bot)

    router = Router()
    register_handlers(
        router,
        BotDeps(
            store=store,
            client=client,
            sender=sender,
            default_dest=settings.default_dest,
            max_active_trackings=settings.max_active_trackings,
            regions=settings.regions,
        ),
    )
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    launch_polling = start_bot or functools.partial(start_bot_polling, bot=bot)

    if stop is None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            for sig in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(NotImplementedError):
                    loop.add_signal_handler(sig, stop.set)

    bot_task = asyncio.create_task(launch_polling(dispatcher), name="bot")
    worker_task = asyncio.create_task(
        run_check_cycle_loop(
            store,
            client,
            sender,
            settings,
            now_fn=_utcnow,
            sleep=asyncio.sleep,
        ),
        name="worker",
    )

    try:
        await stop.wait()
    finally:
        logger.info("shutdown requested, cancelling tasks")
        bot_task.cancel()
        worker_task.cancel()
        await asyncio.gather(bot_task, worker_task, return_exceptions=True)
        if http_client is not None:
            await http_client.aclose()
        if engine is not None:
            await engine.dispose()
        if bot_created:
            # session.close() (не Bot.close() — это API-метод Telegram); идемпотентно
            # относительно закрытия сессии самим start_polling.
            await bot.session.close()
        logger.info("shutdown complete")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    settings = load_settings()
    logger.info("configuration loaded")
    try:
        asyncio.run(run_app(settings))
    except KeyboardInterrupt:
        logger.info("interrupted, shutting down")


if __name__ == "__main__":
    main()
