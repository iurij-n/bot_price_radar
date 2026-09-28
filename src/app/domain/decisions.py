# -*- coding: utf-8 -*-
"""domain/decisions.py — пересечение Порога уведомления (docs/architecture.md §2.3).

Чистая функция: только арифметический факт «Финальная цена относительно Цены
сравнения сдвинулась не меньше Порога». Ничего не знает про подписки, БД и
Telegram; money-инвариант: операнды — int-копейки, float только для Δ%.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class NotificationDecision:
    direction: Literal["up", "down"]
    delta_pct: float


def decide(
    comparison_price_kop: int | None,
    current_price_kop: int | None,
    threshold_pct: float,
) -> NotificationDecision | None:
    """None база (первый замер после add/смены Региона) → None;
    None текущая цена (Недоступный товар — другой сигнал) → None;
    нулевая/отрицательная база → None (защита от деления на ноль);
    |Δ|/база >= Порог (включительно) → решение."""
    if comparison_price_kop is None or current_price_kop is None:
        return None
    if comparison_price_kop <= 0:
        return None
    delta_pct = (current_price_kop - comparison_price_kop) * 100 / comparison_price_kop
    if abs(delta_pct) < threshold_pct:
        return None
    direction: Literal["up", "down"] = "up" if delta_pct > 0 else "down"
    return NotificationDecision(direction=direction, delta_pct=delta_pct)
