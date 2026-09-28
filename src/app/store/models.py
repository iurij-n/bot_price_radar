# -*- coding: utf-8 -*-
"""Таблицы схемы Store: users, products, product_prices, subscriptions,
price_history (ADR-0003, docs/architecture.md §2.2/§3).

Деньги — целые копейки (int). Даты — UTC-aware datetime.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    ForeignKey,
    Index,
    Integer,
    Text,
    TypeDecorator,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

_FMT = "%Y-%m-%d %H:%M:%S.%f"


class UtcDateTime(TypeDecorator):
    """UTC-aware datetime поверх SQLite: храним канонический naive-UTC текст,
    читаем всегда aware. Строковый формат леxicographically монотонен — сравнения
    `< cutoff` в SQL работают корректно."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).strftime(_FMT)

    def process_result_value(self, value: str | None, dialect) -> datetime | None:
        if value is None:
            return None
        return datetime.strptime(value, _FMT).replace(tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    dest: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column(default="active")  # active | inactive


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("marketplace", "nm_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    marketplace: Mapped[str] = mapped_column()
    nm_id: Mapped[int] = mapped_column(BigInteger)
    name: Mapped[str | None] = mapped_column(nullable=True)
    photo_url: Mapped[str | None] = mapped_column(nullable=True)
    link: Mapped[str] = mapped_column()
    first_seen: Mapped[datetime] = mapped_column(UtcDateTime())


class ProductPrice(Base):
    __tablename__ = "product_prices"
    __table_args__ = (UniqueConstraint("product_id", "dest"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    dest: Mapped[str] = mapped_column()
    final_price_kop: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    availability: Mapped[bool] = mapped_column(default=True)
    last_checked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    unavailable_since: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (UniqueConstraint("user_id", "product_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    last_notified_price_kop: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(default="active")  # active | inactive
    unavailable_notified: Mapped[bool] = mapped_column(default=False)


class PriceHistory(Base):
    __tablename__ = "price_history"
    __table_args__ = (Index("ix_price_history_product_dest", "product_id", "dest"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    dest: Mapped[str] = mapped_column()
    price_kop: Mapped[int] = mapped_column(BigInteger)
    recorded_at: Mapped[datetime] = mapped_column(UtcDateTime())
