# -*- coding: utf-8 -*-
"""Worker: Цикл проверки (docs/architecture.md §2.6).

Оркестратор трёх швов: Store / MarketplaceClient / NotifySender. Ничего не
знает про SQL, JSON WB и Telegram-исключения; зависимости инъектируются,
поэтому одна итерация (`run_check_cycle`) тестируется подсовыванием фейков,
а глобальный ритм (`run_check_cycle_loop`) — подсовыванием sleep/rng/now_fn.

Порядок шагов одной итерации (§2.6 1–6): active_dests → tracked_nms →
fetch_cards_batch → apply_batch → notification_candidates → decide → send →
commit/deactivate → unavailable_due → send/commit → prune_history.

Ошибка MarketplaceError на одном Регионе = лог + пропуск этого Региона до
следующего Цикла, без ретраев, остальные Регионы продолжают обработку
(ADR-кандидат §7.7). Failed доставка = без commit: Цена сравнения не
сдвинется, попытка повторится следующим Циклом (spec, история 26).

Композиция (подключение к main.py) — отдельный шаг, main.py здесь не трогаем.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Protocol, Sequence

from app.config import Settings
from app.domain.decisions import decide
from app.marketplaces.base import MarketplaceClient, MarketplaceError
from app.notify.interface import (
    Delivered,
    DeliveryResult,
    Failed,
    NotifySender,
    OutgoingMessage,
    UserBlocked,
)
from app.store.repository import NotifyCandidate, Store, UnavailableCandidate

logger = logging.getLogger("app.workers.cycle")

BUTTON_LABEL = "Открыть товар"

SleepFn = Callable[[float], Awaitable[None]]
NowFn = Callable[[], datetime]

_RU_MINUS_SIGN = "\u2212"


# ------------------------------------------------------------------ формат


def format_price_kop(price_kop: int) -> str:
    """Копейки → «5 990 ₽»; целые рубли без дробей, иначе «5 990,50 ₽».

    Разделитель тысяч — пробел, дробной части — запятая (ru-формат).
    """
    rubles, kopects = divmod(int(price_kop), 100)
    rubles_str = f"{rubles:,}".replace(",", " ")
    if kopects:
        return f"{rubles_str},{kopects:02d} ₽"
    return f"{rubles_str} ₽"


def format_delta_pct(delta_pct: float) -> str:
    """Δ% со знаком и одним знаком после запятой: «−1,2%» / «+1,2%»."""
    sign = _RU_MINUS_SIGN if delta_pct < 0 else "+"
    value = f"{abs(delta_pct):.1f}".replace(".", ",")
    return f"{sign}{value}%"


def _display_name(name: str | None, nm_id: int) -> str:
    return name if name else f"артикул {nm_id}"


def price_change_text(
    name: str | None,
    nm_id: int,
    delta_pct: float,
    comparison_price_kop: int,
    new_price_kop: int,
) -> str:
    return (
        f'Цена на товар "{_display_name(name, nm_id)}" изменилась на '
        f"{format_delta_pct(delta_pct)}: "
        f"{format_price_kop(comparison_price_kop)} → {format_price_kop(new_price_kop)}"
    )


def unavailable_text(name: str | None, nm_id: int) -> str:
    return (
        f'Товар "{_display_name(name, nm_id)}" недоступен в вашем регионе. '
        "Отслеживание сохранено — сообщим, когда цена вернётся."
    )


# ------------------------------------------------------------------- цикл


async def _process_price_candidate(
    store: Store,
    sender: NotifySender,
    candidate: NotifyCandidate,
    threshold_pct: float,
) -> None:
    decision = decide(
        candidate.comparison_price_kop,
        candidate.new_price_kop,
        threshold_pct,
    )
    if decision is None:
        return
    # решаем только при непустых операндах (инвариант decide), фиксируем для типов
    comparison_kop = candidate.comparison_price_kop
    new_kop = candidate.new_price_kop
    assert comparison_kop is not None and new_kop is not None
    message = OutgoingMessage(
        chat_id=candidate.telegram_id,
        text=price_change_text(
            candidate.name,
            candidate.nm_id,
            decision.delta_pct,
            comparison_kop,
            new_kop,
        ),
        button=(BUTTON_LABEL, candidate.link),
    )
    result = await sender.send(message)
    _log_delivery_outcome(result, candidate.subscription_id, candidate.telegram_id)
    if isinstance(result, Delivered):
        await store.commit_notification(candidate.subscription_id, new_kop)
    elif isinstance(result, UserBlocked):
        await store.deactivate_user(candidate.telegram_id)


async def _process_unavailable_candidate(
    store: Store, sender: NotifySender, candidate: UnavailableCandidate
) -> None:
    message = OutgoingMessage(
        chat_id=candidate.telegram_id,
        text=unavailable_text(candidate.name, candidate.nm_id),
        button=(BUTTON_LABEL, candidate.link),
    )
    result = await sender.send(message)
    _log_delivery_outcome(result, candidate.subscription_id, candidate.telegram_id)
    if isinstance(result, Delivered):
        await store.commit_notification(
            candidate.subscription_id, None, mark_unavailable_notified=True
        )
    elif isinstance(result, UserBlocked):
        await store.deactivate_user(candidate.telegram_id)


def _log_delivery_outcome(
    result: DeliveryResult, subscription_id: int, telegram_id: int
) -> None:
    if isinstance(result, Failed):
        logger.warning(
            "доставка не удалась (subscription=%s, telegram_id=%s): %s; "
            "Цена сравнения не сдвинута, попытка повторится следующим Циклом",
            subscription_id,
            telegram_id,
            result.exc_summary,
        )


async def run_check_cycle(
    store: Store,
    client: MarketplaceClient,
    sender: NotifySender,
    settings: Settings,
    *,
    now_fn: NowFn,
    sleep: SleepFn,
) -> None:
    """ОДНА итерация Цикла проверки (§2.6, шаги 1–6).

    `now_fn` — инъектируемые часы; `sleep` принят для единообразия сигнатуры
    с `run_check_cycle_loop`: ритм (45–75 мин) принадлежит циклу, не итерации,
    и внутри одной итерации сон не используется.
    """
    now = now_fn()

    for dest in sorted(await store.active_dests()):
        nms: Sequence[int] = await store.tracked_nms(dest)
        try:
            result = await client.fetch_cards_batch(nms, dest)
        except MarketplaceError:
            logger.warning(
                "Цикл проверки: dest %r — ошибка маркетплейса, "
                "пропускаю регион до следующего Цикла (без ретраев)",
                dest,
                exc_info=True,
            )
            continue

        diff = await store.apply_batch(dest, result, now)
        for candidate in await store.notification_candidates(dest, diff):
            await _process_price_candidate(
                store, sender, candidate, settings.threshold_pct
            )

    window = timedelta(hours=settings.unavailable_window_hours)
    for candidate in await store.unavailable_due(now, window):
        await _process_unavailable_candidate(store, sender, candidate)

    await store.prune_history()


class _Random(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


async def run_check_cycle_loop(
    store: Store,
    client: MarketplaceClient,
    sender: NotifySender,
    settings: Settings,
    *,
    now_fn: NowFn,
    sleep: SleepFn,
    rng: _Random = random,
) -> None:
    """Глобальный ритм: сон rand(cycle_min..cycle_max) минут → одна итерация.

    Сон, часы и генератор диапазона инъектируемы — воркер-задача в main.py
    подставит asyncio.sleep / datetime.now(timezone.utc) / random при
    компоновке (отдельный шаг).
    """
    while True:
        minutes = rng.uniform(
            settings.cycle_min_sleep_minutes, settings.cycle_max_sleep_minutes
        )
        logger.info("Цикл проверки: сон %.1f мин", minutes)
        await sleep(minutes * 60)
        await run_check_cycle(
            store, client, sender, settings, now_fn=now_fn, sleep=sleep
        )
