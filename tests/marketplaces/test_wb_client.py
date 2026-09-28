import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlencode, urlparse, urlunparse

import pytest

from app.marketplaces.base import BatchResult, CardSnapshot, MarketplaceError
from app.marketplaces.http_client import HttpResponse
from app.marketplaces.wb import WBClient

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

DEST = "-1257786"


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakeRequest:
    url: str
    headers: dict[str, str]
    params: dict[str, Any]

    @property
    def full_url(self) -> str:
        if self.params:
            return f"{self.url}?{urlencode(self.params)}"


class FakeHttpClient:
    def __init__(
        self,
        responses: list[HttpResponse] | None = None,
        handler: Callable[[FakeRequest], HttpResponse] | None = None,
    ) -> None:
        self.requests: list[FakeRequest] = []
        self._responses = list(responses or [])
        self._handler = handler

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> HttpResponse:
        request = FakeRequest(
            url=url,
            headers=headers or {},
            params=params or {},
        )
        self.requests.append(request)
        if self._handler is not None:
            return self._handler(request)
        return self._responses.pop(0) if self._responses else HttpResponse(200, b'{"products": []}')


def make_client(
    responses: list[HttpResponse] | None = None,
    handler: Callable[[FakeRequest], HttpResponse] | None = None,
):
    fake = FakeHttpClient(responses=responses, handler=handler)
    return WBClient(client=fake), fake.requests


class TestMarketplaceKey:
    def test_marketplace_is_wb(self):
        client, _ = make_client()
        assert client.marketplace == "wb"


class TestCardParsing:
    async def test_normal_card_yields_final_price_in_kopecks(self):
        payload = fixture("card_normal.json")
        client, _ = make_client([HttpResponse(200, json.dumps(payload).encode())])
        result = await client.fetch_cards_batch([123456789], DEST)
        card = result.cards[0]
        assert card == CardSnapshot(
            nm_id=123456789,
            name="Носки женские, 3 пары",
            photo_url="/images/big/123456789.jpg",
            link="https://www.wildberries.ru/catalog/123456789/detail.aspx",
            final_price_kop=5990000,
            available=True,
        )

    async def test_card_without_price_is_unavailable_not_missing(self):
        payload = fixture("card_unavailable.json")
        client, _ = make_client([HttpResponse(200, json.dumps(payload).encode())])
        result = await client.fetch_cards_batch([223456789], DEST)
        assert result.missing == frozenset()
        card = result.cards[0]
        assert card.final_price_kop is None
        assert card.available is False

    async def test_zero_total_quantity_with_price_is_not_available(self):
        payload = {
            "products": [
                {
                    "id": 333,
                    "name": "Тест",
                    "totalQuantity": 0,
                    "sizes": [{"price": {"product": 10000}}],
                }
            ]
        }
        client, _ = make_client([HttpResponse(200, json.dumps(payload).encode())])
        result = await client.fetch_cards_batch([333], DEST)
        assert result.cards[0].available is False

    async def test_missing_products_cover_the_invariant(self):
        payload = fixture("partial.json")
        client, _ = make_client([HttpResponse(200, json.dumps(payload).encode())])
        result = await client.fetch_cards_batch([123456789, 223456789, 999999999], DEST)
        assert result.missing == frozenset({999999999})
        assert {c.nm_id for c in result.cards} == {123456789, 223456789}
        assert {c.nm_id for c in result.cards} | set(result.missing) == {
            123456789,
            223456789,
            999999999,
        }

    async def test_empty_products_all_missing(self):
        payload = fixture("empty.json")
        client, _ = make_client([HttpResponse(200, json.dumps(payload).encode())])
        result = await client.fetch_cards_batch([111111, 222222], DEST)
        assert result.cards == ()
        assert result.missing == frozenset({111111, 222222})


class TestRequestShape:
    async def test_required_query_params_and_nm_semicolon(self):
        payload = fixture("empty.json")
        client, requests = make_client([HttpResponse(200, json.dumps(payload).encode())])
        await client.fetch_cards_batch([111111, 222222], DEST)
        assert requests[0].url == "https://card.wb.ru/cards/v4/detail"
        params = requests[0].params
        assert params["appType"] == "1"
        assert params["curr"] == "rub"
        assert params["locale"] == "ru"
        assert params["spp"] == "30"
        assert params["dest"] == DEST
        assert params["nm"] == "111111;222222"

    async def test_realistic_user_agent_sent(self):
        payload = fixture("empty.json")
        client, requests = make_client([HttpResponse(200, json.dumps(payload).encode())])
        await client.fetch_cards_batch([111111], DEST)
        ua = requests[0].headers["User-Agent"]
        assert "Mozilla" in ua

    async def test_batch_over_1000_is_split_into_requests(self):
        items = list(range(100000, 100000 + 2500))

        def echo_handler(request: FakeRequest) -> HttpResponse:
            nms = [int(x) for x in request.params["nm"].split(";")]
            payload = {
                "products": [
                    {"id": nm, "name": f"t{nm}", "totalQuantity": 1,
                     "sizes": [{"price": {"product": 100}}]}
                    for nm in nms
                ]
            }
            return HttpResponse(200, json.dumps(payload).encode())

        client, requests = make_client(handler=echo_handler)
        result = await client.fetch_cards_batch(items, DEST)
        assert [len(r.params["nm"].split(";")) for r in requests] == [1000, 1000, 500]
        assert len(result.cards) == 2500
        assert result.missing == frozenset()


class TestDestValidation:
    async def test_empty_dest_raises_before_any_request(self):
        client, requests = make_client()
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], "")
        assert requests == []

    async def test_whitespace_dest_raises(self):
        client, requests = make_client()
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], "   ")
        assert requests == []


class TestErrorMapping:
    async def test_http_400_raises_marketplace_error(self):
        client, _ = make_client([HttpResponse(400, b"")])
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], "-1")

    async def test_http_500_raises_marketplace_error(self):
        client, _ = make_client([HttpResponse(500, b"")])
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], DEST)

    async def test_broken_json_raises_marketplace_error(self):
        client, _ = make_client([HttpResponse(200, b"{not json")])
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], DEST)

    async def test_network_error_raises_marketplace_error(self):
        from app.marketplaces.http_client import HTTPError

        def boom(request: FakeRequest) -> HttpResponse:
            raise HTTPError("connection refused")

        client, _ = make_client(handler=boom)
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], DEST)

    async def test_no_retries_single_request_per_batch(self):
        client, requests = make_client([HttpResponse(500, b"")])
        with pytest.raises(MarketplaceError):
            await client.fetch_cards_batch([111111], DEST)
        assert len(requests) == 1


class TestFactoryInjection:
    def test_accepts_client_injection(self):
        fake = FakeHttpClient()
        client = WBClient(client=fake)
        assert client.marketplace == "wb"


class TestResultTypes:
    async def test_batch_result_fields(self):
        payload = fixture("card_normal.json")
        client, _ = make_client([HttpResponse(200, json.dumps(payload).encode())])
        result = await client.fetch_cards_batch([123456789, 42], DEST)
        assert isinstance(result, BatchResult)
        assert isinstance(result.cards, tuple)
        assert isinstance(result.missing, frozenset)
        assert result.missing == frozenset({42})
