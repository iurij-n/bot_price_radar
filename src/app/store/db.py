# -*- coding: utf-8 -*-
"""Engine/session-фабрика Store (aiosqlite), создание схемы при старте.

Путь к БД приходит извне (Settings.database_path в main.py); тесты используют
специальный путь ``:memory:``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.store.models import Base


def make_engine(database_path: str) -> AsyncEngine:
    """Создаёт async-engine SQLite по пути (или ':memory:')."""
    if database_path == ":memory:":
        return create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}")


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    """Создаёт таблицы схемы (владелец схемы — Store, ADR-0003)."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
