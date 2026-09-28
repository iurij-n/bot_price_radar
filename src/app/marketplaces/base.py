from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable


class MarketplaceError(Exception):
    """Ошибка шва маркетплейса: сеть/таймаут/HTTP 4xx-5xx/битый JSON/невалидный dest."""


@dataclass(frozen=True)
class CardSnapshot:
    nm_id: int
    name: str | None
    photo_url: str | None
    link: str
    final_price_kop: int | None
    available: bool


@dataclass(frozen=True)
class BatchResult:
    cards: tuple[CardSnapshot, ...]
    missing: frozenset[int]


@runtime_checkable
class MarketplaceClient(Protocol):
    marketplace: str

    async def fetch_cards_batch(
        self, items: Sequence[int], dest: str
    ) -> BatchResult: ...
