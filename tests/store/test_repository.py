# -*- coding: utf-8 -*-
"""Интеграционные тесты Store поверх sqlite+aiosqlite:///:memory: (ticket 03).

Тесты идут через Interface Store (§2.2 architecture); прямые SELECT из таблиц
допущены только для проверки инвариантов схемы (уникальные пары, история).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from app.marketplaces.base import BatchResult, CardSnapshot
from app.store.db import init_db, make_engine, make_session_factory
from app.store.models import (
    PriceHistory,
    Product,
    ProductPrice,
    Subscription,
)
from app.store.repository import DuplicateTrackingError, SqlStore, Store


def utc(y: int, mo: int, d: int, h: int = 0, mi: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def snap(nm: int, price: int | None, *, name: str = "Товар", available: bool | None = None) -> CardSnapshot:
    return CardSnapshot(
        nm_id=nm,
        name=name,
        photo_url="pics/1.jpg",
        link=f"https://wb.ru/catalog/{nm}",
        final_price_kop=price,
        available=price is not None if available is None else available,
    )


@dataclass
class Env:
    store: SqlStore
    session_factory: object
    engine: object


@pytest.fixture
async def env():
    engine = make_engine(":memory:")
    await init_db(engine)
    sf = make_session_factory(engine)
    yield Env(SqlStore(sf), sf, engine)
    await engine.dispose()


# ---------------------------------------------------------------- Cycle A


async def test_store_satisfies_protocol(env):
    assert isinstance(env.store, Store)


async def test_init_db_creates_five_tables(env):
    from sqlalchemy import inspect as sa_inspect

    def _names(sync_conn):
        return set(sa_inspect(sync_conn).get_table_names())

    async with env.engine.begin() as conn:
        names = await conn.run_sync(_names)
    assert names == {
        "users",
        "products",
        "product_prices",
        "subscriptions",
        "price_history",
    }


async def test_ensure_user_creates_once_and_keeps_dest_on_repeat(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    await store.ensure_user(1001, "moscow")  # существующего не трогаем
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    rows = await store.list_trackings(1001)
    assert len(rows) == 1
    assert rows[0].dest == "kursk"


async def test_get_user_dest_returns_current_dest_or_none(env):
    store = env.store
    assert await store.get_user_dest(404) is None  # неизвестный пользователь
    await store.ensure_user(1001, "kursk")
    assert await store.get_user_dest(1001) == "kursk"
    await store.set_region(1001, "moscow", {})
    assert await store.get_user_dest(1001) == "moscow"


async def test_upsert_card_idempotent_and_updates_snapshot(env):
    store = env.store
    pid1 = await store.upsert_card("wb", snap(500, 400, name="Старое имя"))
    pid2 = await store.upsert_card("wb", snap(500, 400, name="Новое имя"))
    assert pid1 == pid2
    async with env.session_factory() as s:
        count = await s.scalar(select(func.count()).select_from(Product))
        name = await s.scalar(select(Product.name).where(Product.id == pid1))
    assert count == 1
    assert name == "Новое имя"


async def test_upsert_card_same_nm_different_marketplace_is_different_product(env):
    store = env.store
    pid_wb = await store.upsert_card("wb", snap(500, 400))
    pid_ozon = await store.upsert_card("ozon", snap(500, 400))
    assert pid_wb != pid_ozon


async def test_add_find_list_remove_tracking_lifecycle(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)

    found = await store.find_tracking(1001, pid)
    assert found is not None
    assert found.nm_id == 500
    assert found.comparison_price_kop == 400
    assert found.status == "active"
    assert found.current_price_kop is None  # цен ещё нет

    await store.remove_tracking(1001, pid)
    assert await store.find_tracking(1001, pid) is None
    assert await store.list_trackings(1001) == ()


async def test_add_tracking_duplicate_raises(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    with pytest.raises(DuplicateTrackingError):
        await store.add_tracking(1001, pid, 300)
    # после удаления можно добавить снова
    await store.remove_tracking(1001, pid)
    await store.add_tracking(1001, pid, 300)
    row = await store.find_tracking(1001, pid)
    assert row is not None and row.comparison_price_kop == 300


async def test_add_tracking_unknown_user_raises(env):
    store = env.store
    pid = await store.upsert_card("wb", snap(500, 400))
    with pytest.raises(ValueError):
        await store.add_tracking(404, pid, 400)


async def test_add_tracking_unknown_product_raises(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    with pytest.raises(ValueError):
        await store.add_tracking(1001, 999, 400)


async def test_active_tracking_count_counts_only_active(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    p1 = await store.upsert_card("wb", snap(500, 400))
    p2 = await store.upsert_card("wb", snap(600, 300))
    assert await store.active_tracking_count(1001) == 0
    await store.add_tracking(1001, p1, 400)
    await store.add_tracking(1001, p2, 300)
    assert await store.active_tracking_count(1001) == 2
    await store.remove_tracking(1001, p1)
    assert await store.active_tracking_count(1001) == 1


async def test_deactivate_user_inactivates_without_deleting(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)

    await store.deactivate_user(1001)
    assert await store.active_tracking_count(1001) == 0
    # строки не удалены (CONTEXT: Неактивный пользователь — отслеживания не удалять)
    row = await store.find_tracking(1001, pid)
    assert row is not None
    assert row.status == "inactive"
    # в активных dest не попадает
    assert "kursk" not in await store.active_dests()
    assert await store.tracked_nms("kursk") == ()


async def test_reactivate_user_restores_trackings_and_prices(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.deactivate_user(1001)

    await store.reactivate_user(1001)
    assert await store.active_tracking_count(1001) == 1
    row = await store.find_tracking(1001, pid)
    assert row is not None and row.status == "active"
    # прежняя Цена сравнения сохранена (spec 34)
    assert row.comparison_price_kop == 400
    assert "kursk" in await store.active_dests()


async def test_deactivate_reactivate_unknown_user_is_noop(env):
    await env.store.deactivate_user(404)
    await env.store.reactivate_user(404)


async def test_remove_unknown_tracking_is_noop(env):
    await env.store.ensure_user(1001, "kursk")
    await env.store.remove_tracking(1001, 999)


async def test_active_dests_and_tracked_nms_only_active(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    await store.ensure_user(1002, "moscow")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.add_tracking(1002, pid, 400)

    assert await store.active_dests() == frozenset({"kursk", "moscow"})
    assert set(await store.tracked_nms("kursk")) == {500}

    await store.deactivate_user(1001)
    assert await store.active_dests() == frozenset({"moscow"})

    await store.remove_tracking(1002, pid)
    assert await store.active_dests() == frozenset()


async def test_subscription_unique_pair_enforced_in_db(env):
    """Прямая вставка дубля пары user×product ловится уникальным индексом."""
    from sqlalchemy.exc import IntegrityError

    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    async with env.session_factory.begin() as session:
        user_id = await session.scalar(select(Subscription.user_id).limit(1))
        session.add(
            Subscription(
                user_id=user_id,
                product_id=pid,
                status="active",
                last_notified_price_kop=1,
                unavailable_notified=False,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


# ---------------------------------------------------------------- Cycle B: apply_batch


def batch(*snaps: CardSnapshot, missing: tuple[int, ...] = ()) -> BatchResult:
    return BatchResult(cards=tuple(snaps), missing=frozenset(missing))


async def history_rows(env, product_id: int | None = None, dest: str | None = None):
    async with env.session_factory() as s:
        stmt = select(PriceHistory).order_by(PriceHistory.id)
        if product_id is not None:
            stmt = stmt.where(PriceHistory.product_id == product_id)
        if dest is not None:
            stmt = stmt.where(PriceHistory.dest == dest)
        return list((await s.execute(stmt)).scalars())


async def price_row(env, product_id: int, dest: str):
    async with env.session_factory() as s:
        return await s.scalar(
            select(ProductPrice).where(
                ProductPrice.product_id == product_id, ProductPrice.dest == dest
            )
        )


async def test_apply_batch_new_pair_creates_price_row_and_history(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)

    diff = await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    assert [c.product_id for c in diff.changed] == [pid]
    assert diff.changed[0].old_price_kop is None
    assert diff.changed[0].new_price_kop == 400

    row = await price_row(env, pid, "kursk")
    assert row.final_price_kop == 400
    assert row.availability is True
    assert row.unavailable_since is None
    assert row.last_checked_at == utc(2026, 9, 28, 12)

    rows = await history_rows(env, pid)
    assert [(r.price_kop, r.recorded_at) for r in rows] == [(400, utc(2026, 9, 28, 12))]


async def test_apply_batch_unknown_nm_is_skipped(env):
    """Карточка не в БД (никто её не отслеживает) — никаких строк и ошибок."""
    store = env.store
    diff = await store.apply_batch("kursk", batch(snap(777, 100)), utc(2026, 9, 28, 12))
    assert diff.is_empty()
    async with env.session_factory() as s:
        assert await s.scalar(select(func.count()).select_from(ProductPrice)) == 0


async def test_apply_batch_does_not_cross_marketplace(env):
    store = env.store
    pid_wb = await store.upsert_card("wb", snap(500, 400))
    pid_ozon = await store.upsert_card("ozon", snap(500, 400))
    diff = await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    assert [c.product_id for c in diff.changed] == [pid_wb]
    assert await price_row(env, pid_ozon, "kursk") is None


async def test_apply_batch_idempotent_same_snapshot(env):
    store = env.store
    pid = await store.upsert_card("wb", snap(500, 400))
    first = await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    assert not first.is_empty()

    again = await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 13))
    assert again.is_empty()
    rows = await history_rows(env, pid)
    assert len(rows) == 1
    row = await price_row(env, pid, "kursk")
    assert row.last_checked_at == utc(2026, 9, 28, 13)


async def test_apply_batch_price_change_writes_history(env):
    store = env.store
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    diff = await store.apply_batch("kursk", batch(snap(500, 300)), utc(2026, 9, 28, 13))
    assert diff.changed[0].old_price_kop == 400
    assert diff.changed[0].new_price_kop == 300
    rows = await history_rows(env, pid)
    assert [r.price_kop for r in rows] == [400, 300]


async def test_apply_batch_became_unavailable_and_back(env):
    store = env.store
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))

    d1 = await store.apply_batch("kursk", batch(snap(500, None)), utc(2026, 9, 28, 13))
    assert [c.product_id for c in d1.became_unavailable] == [pid]
    row = await price_row(env, pid, "kursk")
    assert row.availability is False
    assert row.unavailable_since == utc(2026, 9, 28, 13)

    # повторный unavailable-батч: transition не повторяется, since не сдвигается
    d2 = await store.apply_batch("kursk", batch(snap(500, None)), utc(2026, 9, 28, 14))
    assert d2.is_empty()
    row = await price_row(env, pid, "kursk")
    assert row.unavailable_since == utc(2026, 9, 28, 13)

    d3 = await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 15))
    assert [c.product_id for c in d3.became_available] == [pid]
    assert d3.changed == ()  # цена та же — дубль history не пишем
    row = await price_row(env, pid, "kursk")
    assert row.unavailable_since is None
    assert row.availability is True
    rows = await history_rows(env, pid)
    assert [r.price_kop for r in rows] == [400]  # None в историю не пишется


async def test_apply_batch_available_price_different_after_unavailable(env):
    store = env.store
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    await store.apply_batch("kursk", batch(snap(500, None)), utc(2026, 9, 28, 13))
    diff = await store.apply_batch("kursk", batch(snap(500, 300)), utc(2026, 9, 28, 14))
    assert [c.product_id for c in diff.became_available] == [pid]
    assert [c.new_price_kop for c in diff.changed] == [300]
    rows = await history_rows(env, pid)
    assert [r.price_kop for r in rows] == [400, 300]


async def test_apply_batch_missing_nm_marks_unavailable(env):
    store = env.store
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    diff = await store.apply_batch(
        "kursk", batch(missing=(500,)), utc(2026, 9, 28, 13)
    )
    assert [c.product_id for c in diff.became_unavailable] == [pid]
    row = await price_row(env, pid, "kursk")
    assert row.availability is False
    assert row.unavailable_since == utc(2026, 9, 28, 13)


# ---------------------------------------------------------------- Cycle B: candidates


async def test_notification_candidates_only_active_in_dest(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    await store.ensure_user(1002, "kursk")
    await store.ensure_user(1003, "moscow")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.add_tracking(1002, pid, 400)
    await store.add_tracking(1003, pid, 400)

    diff = await store.apply_batch("kursk", batch(snap(500, 300)), utc(2026, 9, 28, 13))
    candidates = await store.notification_candidates("kursk", diff)
    assert {c.telegram_id for c in candidates} == {1001, 1002}
    c = candidates[0]
    assert c.nm_id == 500
    assert c.comparison_price_kop == 400
    assert c.new_price_kop == 300
    assert c.name == "Товар"
    assert c.link == "https://wb.ru/catalog/500"

    await store.deactivate_user(1002)
    candidates = await store.notification_candidates("kursk", diff)
    assert {c.telegram_id for c in candidates} == {1001}


async def test_notification_candidates_empty_without_changes(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    diff = await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 13))
    assert diff.is_empty()
    assert await store.notification_candidates("kursk", diff) == ()


# ---------------------------------------------------------------- Cycle B: unavailable_due / commit


async def seed_unavailable(store, env, since_hour=12):
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 10))
    await store.apply_batch("kursk", batch(snap(500, None)), utc(2026, 9, 28, since_hour))
    return pid


async def test_unavailable_due_window_boundary(env):
    store = env.store
    pid = await seed_unavailable(store, env)
    window = timedelta(hours=48)
    due = await store.unavailable_due(utc(2026, 9, 28, 13), window)
    assert due == ()  # не «older than window»
    due = await store.unavailable_due(utc(2026, 10, 1, 1, 1), window)  # 48:01
    assert len(due) == 1
    assert due[0].telegram_id == 1001
    assert due[0].nm_id == 500
    assert due[0].unavailable_since == utc(2026, 9, 28, 12)


async def test_unavailable_due_skips_notified_and_inactive(env):
    store = env.store
    pid = await seed_unavailable(store, env)
    sub_id = (await store.list_trackings(1001))[0].subscription_id
    now = utc(2026, 10, 5)
    await store.commit_notification(sub_id, None, mark_unavailable_notified=True)
    assert await store.unavailable_due(now, timedelta(hours=48)) == ()
    # неактивный пользователь не досылается
    await store.commit_notification(sub_id, None, mark_unavailable_notified=False)
    await store.deactivate_user(1001)
    assert await store.unavailable_due(now, timedelta(hours=48)) == ()


async def test_unavailable_due_resets_when_price_returns(env):
    store = env.store
    pid = await seed_unavailable(store, env)
    now = utc(2026, 10, 5)
    assert len(await store.unavailable_due(now, timedelta(hours=48))) == 1
    # цена вернулась → unavailable_since снят → ни due, ни transition-дубля
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 16))
    assert await store.unavailable_due(now, timedelta(hours=48)) == ()


async def test_commit_notification_price_resets_unavailable_flag(env):
    store = env.store
    pid = await seed_unavailable(store, env)
    sub_id = (await store.list_trackings(1001))[0].subscription_id
    await store.commit_notification(sub_id, None, mark_unavailable_notified=True)
    # цена вернулась и уведомление о ней закоммичено: база сдвинута, флаг сброшен
    await store.commit_notification(sub_id, 400)
    row = await store.find_tracking(1001, pid)
    assert row.comparison_price_kop == 400
    async with env.session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub.unavailable_notified is False


async def test_commit_notification_none_keeps_comparison(env):
    store = env.store
    pid = await seed_unavailable(store, env)
    sub_id = (await store.list_trackings(1001))[0].subscription_id
    await store.commit_notification(sub_id, None, mark_unavailable_notified=True)
    row = await store.find_tracking(1001, pid)
    assert row.comparison_price_kop == 400  # база Недоступного уведомления не сдвигает


# ---------------------------------------------------------------- Cycle B: set_region


async def test_set_region_resets_comparison_and_flag_keeps_prices(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 10))
    before = await price_row(env, pid, "kursk")

    sub_id = (await store.list_trackings(1001))[0].subscription_id
    await store.commit_notification(sub_id, None, mark_unavailable_notified=True)

    await store.set_region(1001, "moscow", {500: 450})

    row = await store.find_tracking(1001, pid)
    assert row.dest == "moscow"
    assert row.comparison_price_kop == 450
    after = await price_row(env, pid, "kursk")
    assert after.id == before.id
    assert after.final_price_kop == 400
    assert after.availability == before.availability
    assert after.unavailable_since == before.unavailable_since
    async with env.session_factory() as s:
        sub = await s.get(Subscription, sub_id)
        assert sub.unavailable_notified is False


async def test_set_region_none_price_sets_null_comparison(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.set_region(1001, "moscow", {500: None})
    row = await store.find_tracking(1001, pid)
    assert row.comparison_price_kop is None
    # новогоdest ещё нет в ценах — строка product_prices не создана set_region'ом
    assert await price_row(env, pid, "moscow") is None


# ---------------------------------------------------------------- Cycle B: prune


async def test_remove_tracking_prunes_history_only_at_zero(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    await store.ensure_user(1002, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.add_tracking(1002, pid, 400)
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    assert len(await history_rows(env, pid)) == 1

    await store.remove_tracking(1001, pid)
    assert len(await history_rows(env, pid)) == 1  # осталось другое отслеживание

    await store.remove_tracking(1002, pid)
    assert await history_rows(env, pid) == []
    # товары и цены не удаляются никогда
    assert await price_row(env, pid, "kursk") is not None
    async with env.session_factory() as s:
        assert await s.get(Product, pid) is not None


async def test_remove_tracking_keeps_history_for_inactive_subscription(env):
    """Неактивное (в т.ч. чужое) отслеживание — подписка есть, history не трогаем."""
    store = env.store
    await store.ensure_user(1001, "kursk")
    await store.ensure_user(1002, "kursk")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.add_tracking(1002, pid, 400)
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))

    await store.deactivate_user(1002)
    await store.remove_tracking(1001, pid)
    assert len(await history_rows(env, pid)) == 1  # неактивная подписка 1002 осталась


async def test_remove_tracking_prunes_only_own_dest(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    await store.ensure_user(1002, "moscow")
    pid = await store.upsert_card("wb", snap(500, 400))
    await store.add_tracking(1001, pid, 400)
    await store.add_tracking(1002, pid, 400)
    await store.apply_batch("kursk", batch(snap(500, 400)), utc(2026, 9, 28, 12))
    await store.apply_batch("moscow", batch(snap(500, 450)), utc(2026, 9, 28, 12))

    await store.remove_tracking(1001, pid)
    assert await history_rows(env, pid, "kursk") == []
    assert len(await history_rows(env, pid, "moscow")) == 1


async def test_prune_history_removes_pairs_without_subscriptions(env):
    store = env.store
    await store.ensure_user(1001, "kursk")
    p1 = await store.upsert_card("wb", snap(500, 400))
    p2 = await store.upsert_card("wb", snap(600, 300))
    await store.add_tracking(1001, p1, 400)
    await store.apply_batch("kursk", batch(snap(500, 400), snap(600, 300)), utc(2026, 9, 28, 12))
    assert len(await history_rows(env)) == 2

    await store.prune_history()  # у p1 есть подписка, у p2 — нет
    rows = await history_rows(env)
    assert [r.product_id for r in rows] == [p1]
    # строки товара и цены p2 остаются
    assert await price_row(env, p2, "kursk") is not None
    async with env.session_factory() as s:
        assert await s.get(Product, p2) is not None
