from __future__ import annotations


def can_add(active_count: int, limit: int) -> bool:
    """Политика лимита активных Отслеживаний: можно добавить ещё одно,
    если текущих строго меньше лимита (limit = 100 из config).

    Граница включительная: active_count == limit -> False.
    """
    return active_count < limit
