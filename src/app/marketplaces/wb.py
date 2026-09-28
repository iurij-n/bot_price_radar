from __future__ import annotations

import json
from typing import Sequence

from app.marketplaces.base import BatchResult, CardSnapshot, MarketplaceError
from app.marketplaces.http_client import AsyncHttpClient, HTTPError, HTTPStatusError

_BATCH_LIMIT = 1000
_DETAIL_URL = "https://card.wb.ru/cards/v4/detail"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
_DEFAULT_TIMEOUT = 15.0


def _parse_card(product: dict) -> CardSnapshot:
    nm_id = int(product["id"])
    prices = [
        size["price"]["product"]
        for size in product.get("sizes") or []
        if isinstance(size.get("price"), dict) and size["price"].get("product") is not None
    ]
    final_price_kop = min(prices) if prices else None
    total_quantity = int(product.get("totalQuantity") or 0)
    name = product.get("name")
    return CardSnapshot(
        nm_id=nm_id,
        name=name if isinstance(name, str) else None,
        photo_url=product.get("coverImage"),
        link=f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx",
        final_price_kop=final_price_kop,
        available=final_price_kop is not None and total_quantity > 0,
    )


class WBClient:
    marketplace = "wb"

    def __init__(
        self,
        client: AsyncHttpClient,
    ) -> None:
        self._client = client

    async def fetch_cards_batch(
        self, items: Sequence[int], dest: str
    ) -> BatchResult:
        if not dest or not dest.strip():
            raise MarketplaceError("dest обязателен и не может быть пустым")

        items = list(dict.fromkeys(int(nm) for nm in items))
        cards: list[CardSnapshot] = []
        found: set[int] = set()

        try:
            for start in range(0, len(items), _BATCH_LIMIT):
                batch = items[start:start + _BATCH_LIMIT]
                payload = await self._request(batch, dest)
                for product in payload.get("products") or []:
                    card = _parse_card(product)
                    if card.nm_id in found:
                        continue
                    found.add(card.nm_id)
                    cards.append(card)
        except MarketplaceError:
            raise
        except HTTPStatusError as exc:
            raise MarketplaceError(f"WB ответил HTTP {exc.status_code}") from exc
        except (HTTPError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise MarketplaceError(f"ошибка запроса к WB: {exc!r}") from exc

        missing = frozenset(set(items) - found)
        return BatchResult(cards=tuple(cards), missing=missing)

    async def _request(self, batch: list[int], dest: str) -> dict:
        params = {
            "appType": "1",
            "curr": "rub",
            "locale": "ru",
            "spp": "30",
            "dest": dest,
            "nm": ";".join(str(nm) for nm in batch),
        }
        response = await self._client.get(
            _DETAIL_URL, params=params, headers={"User-Agent": _USER_AGENT}
        )
        response.raise_for_status()
        return json.loads(response.content)
