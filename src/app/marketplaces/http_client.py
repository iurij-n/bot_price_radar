from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class HttpResponse:
    status_code: int
    content: bytes

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPStatusError(self.status_code)


class HTTPStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class HTTPError(Exception):
    pass


@runtime_checkable
class AsyncHttpClient(Protocol):
    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> HttpResponse: ...


class CurlCffiClient:
    def __init__(self, timeout: float = 15.0) -> None:
        from curl_cffi.requests import AsyncSession

        self._session = AsyncSession(impersonate="chrome", timeout=timeout)

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> HttpResponse:
        from curl_cffi.requests import RequestsError

        try:
            response = await self._session.get(url, headers=headers, params=params)
            return HttpResponse(status_code=response.status_code, content=response.content)
        except RequestsError as exc:
            raise HTTPError(str(exc)) from exc

    async def aclose(self) -> None:
        await self._session.close()
