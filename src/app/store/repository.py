# -*- coding: utf-8 -*-
"""Module Store: Interface на языке домена + SQLite-реализация (ADR-0002/0003).

Каждый публичный метод — одна атомарная транзакция; сессии/UoW наружу не
отдаются. Значения CycleDiff/NotifyCandidate/TrackingRow — часть Interface'а.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.marketplaces.base import BatchResult, CardSnapshot
from app.store.models import PriceHistory, Product, ProductPrice, Subscription, User

STATUS_ACTIVE = "active"
STATUS_INACTIVE = "inactive"


class DuplicateTrackingError(Exception):
    """Отслеживание этой пары пользователь×Товар уже существует."""


@dataclass(frozen=True)
class TrackingRow:
    subscription_id: int
    product_id: int
    marketplace: str
    nm_id: int
    name: str | None
    photo_url: str | None
    link: str
    dest: str
    current_price_kop: int | None
    comparison_price_kop: int | None
    status: str


@dataclass(frozen=True)
class PriceChange:
    product_id: int
    nm_id: int
    dest: str
    old_price_kop: int | None
    new_price_kop: int | None


@dataclass(frozen=True)
class CycleDiff:
    changed: tuple[PriceChange, ...]
    became_available: tuple[PriceChange, ...]
    became_unavailable: tuple[PriceChange, ...]

    def is_empty(self) -> bool:
        return not (self.changed or self.became_available or self.became_unavailable)


@dataclass(frozen=True)
class NotifyCandidate:
    subscription_id: int
    telegram_id: int
    nm_id: int
    comparison_price_kop: int | None
    new_price_kop: int | None
    name: str | None
    link: str


@dataclass(frozen=True)
class UnavailableCandidate:
    subscription_id: int
    telegram_id: int
    nm_id: int
    unavailable_since: datetime
    name: str | None
    link: str


@runtime_checkable
class Store(Protocol):
    # --- пользователи / UI ---
    async def ensure_user(self, telegram_id: int, dest: str) -> None: ...

    async def get_user_dest(self, telegram_id: int) -> str | None: ...

    async def set_region(
        self, telegram_id: int, dest: str, prices: dict[int, int | None]
    ) -> None: ...

    async def upsert_card(self, marketplace: str, snapshot: CardSnapshot) -> int: ...

    async def add_tracking(
        self, telegram_id: int, product_id: int, base_price_kop: int | None
    ) -> None: ...

    async def remove_tracking(self, telegram_id: int, product_id: int) -> None: ...

    async def active_tracking_count(self, telegram_id: int) -> int: ...

    async def list_trackings(self, telegram_id: int) -> tuple[TrackingRow, ...]: ...

    async def find_tracking(self, telegram_id: int, product_id: int) -> TrackingRow | None: ...

    # --- Цикл проверки (воркер) ---
    async def active_dests(self) -> frozenset[str]: ...

    async def tracked_nms(self, dest: str) -> tuple[int, ...]: ...

    async def apply_batch(
        self, dest: str, result: BatchResult, now: datetime
    ) -> CycleDiff: ...

    async def notification_candidates(
        self, dest: str, diff: CycleDiff
    ) -> tuple[NotifyCandidate, ...]: ...

    async def unavailable_due(
        self, now: datetime, window: timedelta
    ) -> tuple[UnavailableCandidate, ...]: ...

    async def commit_notification(
        self,
        subscription_id: int,
        new_comparison_kop: int | None,
        mark_unavailable_notified: bool = False,
    ) -> None: ...

    async def deactivate_user(self, telegram_id: int) -> None: ...

    async def reactivate_user(self, telegram_id: int) -> None: ...

    async def prune_history(self) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SqlStore:
    """Adapter Store поверх SQLAlchemy/aiosqlite (ADR-0002).

    marketplace — площадка, чьи батчи умеет применять этот Adapter (ADR-0004:
    nm одного маркетплейса не смешивается с другим). v1: только "wb".
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        marketplace: str = "wb",
    ) -> None:
        self._sf = session_factory
        self._marketplace = marketplace

    # ---------- Cycle A ----------

    async def ensure_user(self, telegram_id: int, dest: str) -> None:
        async with self._sf() as session, session.begin():
            user_id = await session.scalar(
                select(User.id).where(User.telegram_id == telegram_id)
            )
            if user_id is None:
                try:
                    session.add(
                        User(telegram_id=telegram_id, dest=dest, status=STATUS_ACTIVE)
                    )
                    await session.flush()
                except IntegrityError:
                    pass  # race condition: user created concurrently

    async def get_user_dest(self, telegram_id: int) -> str | None:
        async with self._sf() as session:
            return await session.scalar(
                select(User.dest).where(User.telegram_id == telegram_id)
            )

    async def upsert_card(self, marketplace: str, snapshot: CardSnapshot) -> int:
        async with self._sf() as session, session.begin():
            product = await session.scalar(
                select(Product)
                .where(Product.marketplace == marketplace)
                .where(Product.nm_id == snapshot.nm_id)
            )
            if product is None:
                product = Product(
                    marketplace=marketplace,
                    nm_id=snapshot.nm_id,
                    name=snapshot.name,
                    photo_url=snapshot.photo_url,
                    link=snapshot.link,
                    first_seen=_utcnow(),
                )
                session.add(product)
            else:
                product.name = snapshot.name
                product.photo_url = snapshot.photo_url
                product.link = snapshot.link
            await session.flush()
            return product.id

    async def add_tracking(
        self, telegram_id: int, product_id: int, base_price_kop: int | None
    ) -> None:
        async with self._sf() as session, session.begin():
            user_id = await session.scalar(
                select(User.id).where(User.telegram_id == telegram_id)
            )
            if user_id is None:
                raise ValueError(f"пользователь {telegram_id} не найден")
            product_exists = await session.scalar(
                select(Product.id).where(Product.id == product_id)
            )
            if product_exists is None:
                raise ValueError(f"товар {product_id} не найден")
            duplicate = await session.scalar(
                select(Subscription.id)
                .where(Subscription.user_id == user_id)
                .where(Subscription.product_id == product_id)
            )
            if duplicate is not None:
                raise DuplicateTrackingError(
                    f"отслеживание пары {telegram_id}×{product_id} уже существует"
                )
            session.add(
                Subscription(
                    user_id=user_id,
                    product_id=product_id,
                    status=STATUS_ACTIVE,
                    last_notified_price_kop=base_price_kop,
                    unavailable_notified=False,
                )
            )

    async def remove_tracking(self, telegram_id: int, product_id: int) -> None:
        async with self._sf() as session, session.begin():
            user_id = await session.scalar(
                select(User.id).where(User.telegram_id == telegram_id)
            )
            if user_id is None:
                return
            sub = await session.scalar(
                select(Subscription)
                .where(Subscription.user_id == user_id)
                .where(Subscription.product_id == product_id)
            )
            if sub is None:
                return
            await session.delete(sub)
            await session.flush()
            dest = await session.scalar(select(User.dest).where(User.id == user_id))
            await self._prune_pair(session, product_id, dest)

    @staticmethod
    async def _prune_pair(session: AsyncSession, product_id: int, dest: str) -> None:
        # реализуется циклом B (prune истории)
        return None

    async def active_tracking_count(self, telegram_id: int) -> int:
        async with self._sf() as session:
            count = await session.scalar(
                select(func.count(Subscription.id))
                .join(User, User.id == Subscription.user_id)
                .where(User.telegram_id == telegram_id)
                .where(Subscription.status == STATUS_ACTIVE)
            )
            return int(count or 0)

    async def list_trackings(self, telegram_id: int) -> tuple[TrackingRow, ...]:
        async with self._sf() as session:
            rows = (
                await session.execute(self._tracking_query(telegram_id))
            ).all()
            return tuple(self._to_tracking_row(r) for r in rows)

    async def find_tracking(self, telegram_id: int, product_id: int) -> TrackingRow | None:
        async with self._sf() as session:
            stmt = self._tracking_query(telegram_id).where(Product.id == product_id)
            row = (await session.execute(stmt)).first()
            if row is None:
                return None
            return self._to_tracking_row(row)

    @staticmethod
    def _tracking_query(telegram_id: int):
        return (
            select(
                Subscription.id.label("subscription_id"),
                Product.id.label("product_id"),
                Product.marketplace,
                Product.nm_id,
                Product.name,
                Product.photo_url,
                Product.link,
                User.dest,
                ProductPrice.final_price_kop,
                Subscription.last_notified_price_kop,
                Subscription.status,
            )
            .join(User, User.id == Subscription.user_id)
            .join(Product, Product.id == Subscription.product_id)
            .outerjoin(
                ProductPrice,
                (ProductPrice.product_id == Product.id)
                & (ProductPrice.dest == User.dest),
            )
            .where(User.telegram_id == telegram_id)
            .order_by(Subscription.id)
        )

    @staticmethod
    def _to_tracking_row(r) -> TrackingRow:
        return TrackingRow(
            subscription_id=r.subscription_id,
            product_id=r.product_id,
            marketplace=r.marketplace,
            nm_id=r.nm_id,
            name=r.name,
            photo_url=r.photo_url,
            link=r.link,
            dest=r.dest,
            current_price_kop=r.final_price_kop,
            comparison_price_kop=r.last_notified_price_kop,
            status=r.status,
        )

    async def active_dests(self) -> frozenset[str]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(User.dest)
                    .join(Subscription, Subscription.user_id == User.id)
                    .where(Subscription.status == STATUS_ACTIVE)
                    .where(User.status == STATUS_ACTIVE)
                    .distinct()
                )
            ).all()
            return frozenset(r[0] for r in rows)

    async def tracked_nms(self, dest: str) -> tuple[int, ...]:
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(Product.nm_id)
                    .join(Subscription, Subscription.product_id == Product.id)
                    .join(User, User.id == Subscription.user_id)
                    .where(Subscription.status == STATUS_ACTIVE)
                    .where(User.status == STATUS_ACTIVE)
                    .where(User.dest == dest)
                    .distinct()
                )
            ).all()
            return tuple(r[0] for r in rows)

    async def deactivate_user(self, telegram_id: int) -> None:
        await self._set_user_status(telegram_id, STATUS_INACTIVE)

    async def reactivate_user(self, telegram_id: int) -> None:
        await self._set_user_status(telegram_id, STATUS_ACTIVE)

    async def _set_user_status(self, telegram_id: int, status: str) -> None:
        async with self._sf() as session, session.begin():
            user_id = await session.scalar(
                select(User.id).where(User.telegram_id == telegram_id)
            )
            if user_id is None:
                return
            await session.execute(
                update(User).where(User.id == user_id).values(status=status)
            )
            await session.execute(
                update(Subscription)
                .where(Subscription.user_id == user_id)
                .values(status=status)
            )

    # ---------- Cycle B ----------

    async def set_region(
        self, telegram_id: int, dest: str, prices: dict[int, int | None]
    ) -> None:
        async with self._sf() as session, session.begin():
            user_id = await session.scalar(
                select(User.id).where(User.telegram_id == telegram_id)
            )
            if user_id is None:
                return
            await session.execute(
                update(User).where(User.id == user_id).values(dest=dest)
            )
            sub_rows = (
                await session.execute(
                    select(Subscription.id, Product.nm_id)
                    .join(Product, Product.id == Subscription.product_id)
                    .where(Subscription.user_id == user_id)
                )
            ).all()
            for sub_id, nm_id in sub_rows:
                await session.execute(
                    update(Subscription)
                    .where(Subscription.id == sub_id)
                    .values(
                        last_notified_price_kop=prices.get(nm_id),
                        unavailable_notified=False,
                    )
                )

    async def apply_batch(
        self, dest: str, result: BatchResult, now: datetime
    ) -> CycleDiff:
        async with self._sf() as session, session.begin():
            nm_ids = {c.nm_id for c in result.cards} | set(result.missing)
            products = (
                await session.execute(
                    select(Product).where(
                        Product.marketplace == self._marketplace,
                        Product.nm_id.in_(nm_ids),
                    )
                )
            ).scalars().all()
            by_nm = {p.nm_id: p for p in products}

            changed: list[PriceChange] = []
            became_available: list[PriceChange] = []
            became_unavailable: list[PriceChange] = []

            snapshots = {c.nm_id: c for c in result.cards}
            for nm_id, product in by_nm.items():
                snap = snapshots.get(nm_id)
                new_price = snap.final_price_kop if snap is not None else None
                new_unavailable = snap is None or snap.final_price_kop is None

                row = await session.scalar(
                    select(ProductPrice).where(
                        ProductPrice.product_id == product.id,
                        ProductPrice.dest == dest,
                    )
                )
                if row is None:
                    row = ProductPrice(
                        product_id=product.id,
                        dest=dest,
                        final_price_kop=None if new_unavailable else new_price,
                        availability=not new_unavailable,
                        last_checked_at=now,
                        unavailable_since=now if new_unavailable else None,
                    )
                    session.add(row)
                    if not new_unavailable:
                        assert new_price is not None
                        session.add(
                            PriceHistory(
                                product_id=product.id,
                                dest=dest,
                                price_kop=new_price,
                                recorded_at=now,
                            )
                        )
                        changed.append(
                            PriceChange(product.id, nm_id, dest, None, new_price)
                        )
                    continue

                old_price = row.final_price_kop
                was_unavailable = not row.availability
                row.last_checked_at = now
                if new_unavailable:
                    if not was_unavailable:
                        row.availability = False
                        row.unavailable_since = now
                        became_unavailable.append(
                            PriceChange(product.id, nm_id, dest, old_price, None)
                        )
                else:
                    assert new_price is not None
                    if was_unavailable:
                        row.availability = True
                        row.unavailable_since = None
                        became_available.append(
                            PriceChange(product.id, nm_id, dest, old_price, new_price)
                        )
                    if old_price != new_price:
                        row.final_price_kop = new_price
                        session.add(
                            PriceHistory(
                                product_id=product.id,
                                dest=dest,
                                price_kop=new_price,
                                recorded_at=now,
                            )
                        )
                        changed.append(
                            PriceChange(product.id, nm_id, dest, old_price, new_price)
                        )
            return CycleDiff(
                changed=tuple(changed),
                became_available=tuple(became_available),
                became_unavailable=tuple(became_unavailable),
            )

    async def notification_candidates(
        self, dest: str, diff: CycleDiff
    ) -> tuple[NotifyCandidate, ...]:
        new_prices: dict[int, int] = {}
        for pc in (*diff.changed, *diff.became_available):
            if pc.new_price_kop is not None:
                new_prices[pc.product_id] = pc.new_price_kop
        if not new_prices:
            return ()
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(
                        Subscription.id,
                        User.telegram_id,
                        Product.nm_id,
                        Subscription.last_notified_price_kop,
                        Product.name,
                        Product.link,
                        Product.id,
                    )
                    .join(User, User.id == Subscription.user_id)
                    .join(Product, Product.id == Subscription.product_id)
                    .where(
                        Product.id.in_(new_prices),
                        Subscription.status == STATUS_ACTIVE,
                        User.status == STATUS_ACTIVE,
                        User.dest == dest,
                    )
                    .order_by(Subscription.id)
                )
            ).all()
        return tuple(
            NotifyCandidate(
                subscription_id=r[0],
                telegram_id=r[1],
                nm_id=r[2],
                comparison_price_kop=r[3],
                new_price_kop=new_prices[r[6]],
                name=r[4],
                link=r[5],
            )
            for r in rows
        )

    async def unavailable_due(
        self, now: datetime, window: timedelta
    ) -> tuple[UnavailableCandidate, ...]:
        cutoff = now - window
        async with self._sf() as session:
            rows = (
                await session.execute(
                    select(
                        Subscription.id,
                        User.telegram_id,
                        Product.nm_id,
                        ProductPrice.unavailable_since,
                        Product.name,
                        Product.link,
                    )
                    .join(User, User.id == Subscription.user_id)
                    .join(Product, Product.id == Subscription.product_id)
                    .join(
                        ProductPrice,
                        (ProductPrice.product_id == Product.id)
                        & (ProductPrice.dest == User.dest),
                    )
                    .where(
                        ProductPrice.unavailable_since.is_not(None),
                        ProductPrice.unavailable_since < cutoff,
                        Subscription.unavailable_notified.is_(False),
                        Subscription.status == STATUS_ACTIVE,
                        User.status == STATUS_ACTIVE,
                    )
                    .order_by(Subscription.id)
                )
            ).all()
        return tuple(
            UnavailableCandidate(
                subscription_id=r[0],
                telegram_id=r[1],
                nm_id=r[2],
                unavailable_since=r[3],
                name=r[4],
                link=r[5],
            )
            for r in rows
        )

    async def commit_notification(
        self,
        subscription_id: int,
        new_comparison_kop: int | None,
        mark_unavailable_notified: bool = False,
    ) -> None:
        async with self._sf() as session, session.begin():
            values: dict = {}
            if new_comparison_kop is not None:
                values["last_notified_price_kop"] = new_comparison_kop
                values["unavailable_notified"] = False
            if mark_unavailable_notified:
                values["unavailable_notified"] = True
            if values:
                await session.execute(
                    update(Subscription)
                    .where(Subscription.id == subscription_id)
                    .values(**values)
                )

    async def prune_history(self) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                delete(PriceHistory).where(
                    ~exists(
                        select(Subscription.id)
                        .join(User, User.id == Subscription.user_id)
                        .where(
                            Subscription.product_id == PriceHistory.product_id,
                            User.dest == PriceHistory.dest,
                        )
                    )
                )
            )

    @staticmethod
    async def _prune_pair(session: AsyncSession, product_id: int, dest: str) -> None:
        remaining = await session.scalar(
            select(Subscription.id)
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.product_id == product_id,
                User.dest == dest,
            )
            .limit(1)
        )
        if remaining is None:
            await session.execute(
                delete(PriceHistory).where(
                    PriceHistory.product_id == product_id,
                    PriceHistory.dest == dest,
                )
            )
