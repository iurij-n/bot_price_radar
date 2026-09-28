# -*- coding: utf-8 -*-
"""Воркер Цикла проверки на фейках трёх швов + реальном domain (spec, п.5–6).

Тесты идут через Interface: fake Store (программируемый diff/кандидаты), fake
MarketplaceClient (готовый BatchResult или MarketplaceError), fake NotifySender
(скриптуемый DeliveryResult). Порядок diff → decide → send → commit проверяется
по общему журналу вызовов.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.marketplaces.base import BatchResult, CardSnapshot, MarketplaceError
from app.notify.interface import (
    Delivered,
    DeliveryResult,
    Failed,
    OutgoingMessage,
    UserBlocked,
)
from app.store.repository import (
    CycleDiff,
    NotifyCandidate,
    PriceChange,
    UnavailableCandidate,
)
from app.workers.cycle import (
    format_delta_pct,
    format_price_kop,
    price_change_text,
    run_check_cycle,
    run_check_cycle_loop,
    unavailable_text,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
EMPTY_DIFF = CycleDiff(changed=(), became_available=(), became_unavailable=())


def make_settings(**overrides) -> Settings:
    base = dict(
        bot_token="t",
        default_dest="1",
        threshold_pct=0.5,
        unavailable_window_hours=48,
        cycle_min_sleep_minutes=45,
        cycle_max_sleep_minutes=75,
    )
    base.update(overrides)
    return Settings(**base)


SETTINGS = make_settings()


# ------------------------------------------------------------------- фейки


class FakeStore:
    def __init__(self, journal: list[str]) -> None:
        self.journal = journal
        self.dests: frozenset[str] = frozenset()
        self.nms_by_dest: dict[str, tuple[int, ...]] = {}
        self.diff_by_dest: dict[str, CycleDiff] = {}
        self.candidates_by_dest: dict[str, tuple[NotifyCandidate, ...]] = {}
        self.unavailable_result: tuple[UnavailableCandidate, ...] = ()
        self.commits: list[tuple[int, int | None, bool]] = []
        self.deactivated: list[int] = []
        self.prune_calls = 0
        self.apply_calls: list[tuple[str, BatchResult, datetime]] = []
        self.unavailable_windows: list[timedelta] = []
        self.unavailable_nows: list[datetime] = []

    async def active_dests(self) -> frozenset[str]:
        self.journal.append("active_dests")
        return self.dests

    async def tracked_nms(self, dest: str) -> tuple[int, ...]:
        self.journal.append(f"tracked_nms:{dest}")
        return self.nms_by_dest.get(dest, ())

    async def apply_batch(
        self, dest: str, result: BatchResult, now: datetime
    ) -> CycleDiff:
        self.journal.append(f"apply_batch:{dest}")
        self.apply_calls.append((dest, result, now))
        return self.diff_by_dest.get(dest, EMPTY_DIFF)

    async def notification_candidates(
        self, dest: str, diff: CycleDiff
    ) -> tuple[NotifyCandidate, ...]:
        self.journal.append(f"notification_candidates:{dest}")
        return self.candidates_by_dest.get(dest, ())

    async def unavailable_due(
        self, now: datetime, window: timedelta
    ) -> tuple[UnavailableCandidate, ...]:
        self.journal.append("unavailable_due")
        self.unavailable_nows.append(now)
        self.unavailable_windows.append(window)
        return self.unavailable_result

    async def commit_notification(
        self,
        subscription_id: int,
        new_comparison_kop: int | None,
        mark_unavailable_notified: bool = False,
    ) -> None:
        self.journal.append(f"commit:{subscription_id}")
        self.commits.append((subscription_id, new_comparison_kop, mark_unavailable_notified))

    async def deactivate_user(self, telegram_id: int) -> None:
        self.journal.append(f"deactivate:{telegram_id}")
        self.deactivated.append(telegram_id)

    async def prune_history(self) -> None:
        self.journal.append("prune_history")
        self.prune_calls += 1


class FakeClient:
    marketplace = "wb"

    def __init__(self, journal: list[str], results: dict[str, object] | None = None):
        self.journal = journal
        self.results = results or {}
        self.calls: list[tuple[tuple[int, ...], str]] = []

    async def fetch_cards_batch(self, items, dest: str) -> BatchResult:
        self.journal.append(f"fetch:{dest}")
        self.calls.append((tuple(items), dest))
        outcome = self.results.get(dest, BatchResult(cards=(), missing=frozenset()))
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, BatchResult)
        return outcome


class FakeSender:
    def __init__(
        self,
        journal: list[str],
        results: dict[int, DeliveryResult] | None = None,
        default: DeliveryResult = Delivered(),
    ):
        self.journal = journal
        self.results = results or {}
        self.default = default
        self.messages: list[OutgoingMessage] = []

    async def send(self, message: OutgoingMessage) -> DeliveryResult:
        self.journal.append(f"send:{message.chat_id}")
        self.messages.append(message)
        return self.results.get(message.chat_id, self.default)


# ---------------------------------------------------------------- хелперы


def snap(nm: int, price: int | None) -> CardSnapshot:
    return CardSnapshot(
        nm_id=nm,
        name="Товар",
        photo_url="pics/1.jpg",
        link=f"https://wb.ru/catalog/{nm}",
        final_price_kop=price,
        available=price is not None,
    )


def candidate(
    sub_id: int = 1,
    telegram_id: int = 10,
    nm_id: int = 123456,
    comparison: int | None = 1_000_000,
    new: int | None = 994_000,
    name: str | None = "Носки",
    link: str = "https://wb.ru/catalog/123456",
) -> NotifyCandidate:
    return NotifyCandidate(
        subscription_id=sub_id,
        telegram_id=telegram_id,
        nm_id=nm_id,
        comparison_price_kop=comparison,
        new_price_kop=new,
        name=name,
        link=link,
    )


def changed(product_id: int, nm_id: int, old: int | None, new: int | None) -> PriceChange:
    return PriceChange(product_id=product_id, nm_id=nm_id, dest="1", old_price_kop=old, new_price_kop=new)


def unavailable_candidate(
    sub_id: int = 3,
    telegram_id: int = 30,
    nm_id: int = 999,
    name: str | None = "Платье",
    link: str = "https://wb.ru/catalog/999",
) -> UnavailableCandidate:
    return UnavailableCandidate(
        subscription_id=sub_id,
        telegram_id=telegram_id,
        nm_id=nm_id,
        unavailable_since=NOW - timedelta(hours=49),
        name=name,
        link=link,
    )


async def _noop_sleep(seconds: float) -> None:
    return None


async def run_once(store, client, sender, settings=SETTINGS) -> None:
    await run_check_cycle(
        store, client, sender, settings, now_fn=lambda: NOW, sleep=_noop_sleep
    )


@dataclass
class Env:
    store: FakeStore
    client: FakeClient
    sender: FakeSender
    journal: list[str]


def make_env(
    *,
    dests: tuple[str, ...] = ("1",),
    nms: dict[str, tuple[int, ...]] | None = None,
    diffs: dict[str, CycleDiff] | None = None,
    candidates: dict[str, tuple[NotifyCandidate, ...]] | None = None,
    client_results: dict[str, object] | None = None,
    sender_results: dict[int, DeliveryResult] | None = None,
) -> Env:
    journal: list[str] = []
    store = FakeStore(journal)
    store.dests = frozenset(dests)
    store.nms_by_dest = nms or {"1": (123456,)}
    store.diff_by_dest = diffs or {}
    store.candidates_by_dest = candidates or {}
    client = FakeClient(journal, client_results)
    sender = FakeSender(journal, sender_results)
    return Env(store, client, sender, journal)


# ------------------------------------------------------------- сценарии spec


async def test_scenario1_price_drop_above_threshold_sends_and_commits():
    env = make_env(
        diffs={"1": CycleDiff(changed=(changed(7, 123456, 1_000_000, 994_000),), became_available=(), became_unavailable=())},
        candidates={"1": (candidate(comparison=1_000_000, new=994_000),)},
    )
    await run_once(env.store, env.client, env.sender)

    assert len(env.sender.messages) == 1
    msg = env.sender.messages[0]
    assert msg.chat_id == 10
    assert msg.text == 'Цена на товар "Носки" изменилась на −0,6%: 10 000 ₽ → 9 940 ₽'
    assert msg.button == ("Открыть товар", "https://wb.ru/catalog/123456")
    assert env.store.commits == [(1, 994_000, False)]


async def test_scenario2_change_below_threshold_is_silent():
    env = make_env(
        diffs={"1": CycleDiff(changed=(changed(7, 123456, 1_000_000, 997_000),), became_available=(), became_unavailable=())},
        candidates={"1": (candidate(comparison=1_000_000, new=997_000),)},
    )
    await run_once(env.store, env.client, env.sender)

    assert env.sender.messages == []
    assert env.store.commits == []
    # база не сдвинута, цикл дошёл до конца
    assert env.store.prune_calls == 1


async def test_scenario3_first_cycle_after_add_comparison_none_no_send():
    env = make_env(
        diffs={"1": CycleDiff(changed=(changed(7, 123456, None, 994_000),), became_available=(), became_unavailable=())},
        candidates={"1": (candidate(comparison=None, new=994_000),)},
    )
    await run_once(env.store, env.client, env.sender)

    assert env.sender.messages == []
    assert env.store.commits == []


async def test_scenario4_marketplace_error_skips_only_failed_dest():
    ok_batch = BatchResult(cards=(snap(111, 1_000_000),), missing=frozenset())
    env = make_env(
        dests=("A", "B"),
        nms={"A": (111,), "B": (111,)},
        client_results={"A": MarketplaceError("429 too many requests"), "B": ok_batch},
        diffs={"B": CycleDiff(changed=(changed(7, 111, 1_000_000, 994_000),), became_available=(), became_unavailable=())},
        candidates={"B": (candidate(nm_id=111),)},
    )
    await run_once(env.store, env.client, env.sender)

    # dest A пропущен до следующего Цикла: без apply_batch, без остальных шагов
    assert [d for d, _, _ in env.store.apply_calls] == ["B"]
    # dest B обработан полностью
    assert env.sender.messages and env.store.commits == [(1, 994_000, False)]
    assert "notification_candidates:A" not in env.journal
    # цикл не упал и дошёл до конца
    assert env.journal[-1] == "prune_history"


async def test_scenario5_failed_delivery_does_not_commit():
    env = make_env(
        diffs={"1": CycleDiff(changed=(changed(7, 123456, 1_000_000, 994_000),), became_available=(), became_unavailable=())},
        candidates={"1": (candidate(comparison=1_000_000, new=994_000),)},
        sender_results={10: Failed(exc_summary="FloodWait exhausted", attempt_count=3)},
    )
    await run_once(env.store, env.client, env.sender)

    assert len(env.sender.messages) == 1
    assert env.store.commits == []
    assert env.store.deactivated == []


async def test_scenario6_user_blocked_deactivates_once():
    env = make_env(
        diffs={"1": CycleDiff(changed=(changed(7, 123456, 1_000_000, 994_000),), became_available=(), became_unavailable=())},
        candidates={"1": (candidate(comparison=1_000_000, new=994_000),)},
        sender_results={10: UserBlocked()},
    )
    await run_once(env.store, env.client, env.sender)

    assert env.store.deactivated == [10]
    assert env.store.commits == []


async def test_scenario7_unavailable_48h_single_notification_then_price_returns():
    env = make_env()
    env.store.unavailable_result = (unavailable_candidate(),)
    await run_once(env.store, env.client, env.sender)

    assert env.store.unavailable_windows == [timedelta(hours=48)]
    assert env.store.unavailable_nows == [NOW]
    assert len(env.sender.messages) == 1
    msg = env.sender.messages[0]
    assert msg.chat_id == 30
    assert msg.text.startswith('Товар "Платье" недоступен в вашем регионе')
    assert msg.button == ("Открыть товар", "https://wb.ru/catalog/999")
    assert env.store.commits == [(3, None, True)]

    # цена вернулась: дальше работает обычный путь decide → send → commit
    env.store.unavailable_result = ()
    env.store.diff_by_dest = {
        "1": CycleDiff(changed=(changed(7, 123456, 990_000, 1_000_000),), became_available=(), became_unavailable=())
    }
    env.store.candidates_by_dest = {"1": (candidate(sub_id=3, telegram_id=30, comparison=990_000, new=1_000_000),)}
    await run_once(env.store, env.client, env.sender)

    assert len(env.sender.messages) == 2
    assert "+1,0%" in env.sender.messages[1].text
    assert env.store.commits[-1] == (3, 1_000_000, False)


# ------------------------------------------------------------ порядок/ритм


async def test_iteration_order_diff_decide_send_commit_then_unavailable_prune():
    env = make_env(
        diffs={"1": CycleDiff(changed=(changed(7, 123456, 1_000_000, 994_000),), became_available=(), became_unavailable=())},
        candidates={"1": (candidate(comparison=1_000_000, new=994_000),)},
    )
    await run_once(env.store, env.client, env.sender)

    assert env.journal == [
        "active_dests",
        "tracked_nms:1",
        "fetch:1",
        "apply_batch:1",
        "notification_candidates:1",
        "send:10",
        "commit:1",
        "unavailable_due",
        "prune_history",
    ]


async def test_cycle_uses_injected_clock_and_batch_flow():
    batch = BatchResult(cards=(snap(123456, 994_000),), missing=frozenset())
    env = make_env(client_results={"1": batch})
    await run_once(env.store, env.client, env.sender)

    assert env.client.calls == [((123456,), "1")]
    assert env.store.apply_calls == [("1", batch, NOW)]


class RecordingRng:
    def __init__(self, value: float):
        self.value = value
        self.calls: list[tuple[float, float]] = []

    def uniform(self, a: float, b: float) -> float:
        self.calls.append((a, b))
        return self.value


class _StopLoop(Exception):
    pass


async def test_loop_sleeps_uniform_minutes_before_each_iteration():
    env = make_env()
    sleeps: list[float] = []

    async def sleep_fn(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise _StopLoop

    rng = RecordingRng(value=60.0)
    with pytest.raises(_StopLoop):
        await run_check_cycle_loop(
            env.store,
            env.client,
            env.sender,
            SETTINGS,
            now_fn=lambda: NOW,
            sleep=sleep_fn,
            rng=rng,
        )

    assert rng.calls == [(45, 75), (45, 75)]
    assert sleeps == [3600.0, 3600.0]
    # после первого сна прошла ровно одна полная итерация
    assert env.journal.count("active_dests") == 1
    assert env.journal.count("prune_history") == 1


# --------------------------------------------------------------- формат


def test_format_price_kop_whole_rubles_thousands_space():
    assert format_price_kop(599_000) == "5 990 ₽"
    assert format_price_kop(5_990_000) == "59 900 ₽"
    assert format_price_kop(1_234_567_800) == "12 345 678 ₽"
    assert format_price_kop(9_900) == "99 ₽"


def test_format_price_kop_with_kopecks():
    assert format_price_kop(599_050) == "5 990,50 ₽"
    assert format_price_kop(95) == "0,95 ₽"


def test_format_delta_pct_sign_and_comma():
    assert format_delta_pct(-1.2) == "−1,2%"
    assert format_delta_pct(1.2) == "+1,2%"
    assert format_delta_pct(-0.5) == "−0,5%"
    assert format_delta_pct(0.0) == "+0,0%"


def test_price_change_text_template():
    text = price_change_text("Носки", 123456, -0.6, 1_000_000, 994_000)
    assert text == 'Цена на товар "Носки" изменилась на −0,6%: 10 000 ₽ → 9 940 ₽'


def test_price_change_text_falls_back_to_article_when_name_missing():
    text = price_change_text(None, 123456, 0.6, 1_000_000, 1_006_000)
    assert text == 'Цена на товар "артикул 123456" изменилась на +0,6%: 10 000 ₽ → 10 060 ₽'


def test_unavailable_text_template():
    text = unavailable_text("Платье", 999)
    assert text.startswith('Товар "Платье" недоступен в вашем регионе')
    assert "артикул" not in text
