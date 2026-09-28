from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlparse

_SkuReason = Literal[None, "not_found", "ambiguous", "short_link"]

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_SKU_TOKEN_RE = re.compile(r"(?<![\dA-Za-z])\d{6,10}(?![\dA-Za-z])")
_CATALOG_PATH_RE = re.compile(r"^/(?:[\d\w-]+/)?(?:catalog|product-card)/(\d{6,10})(?:/|$)", re.IGNORECASE)
_WB_HOSTS = ("wildberries.ru", "wb.ru")


@dataclass(frozen=True)
class SkuParse:
    nm_id: int | None
    reason: _SkuReason


def _is_wb_host(host: str) -> bool:
    host = host.lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in _WB_HOSTS)


def _nm_from_wb_url(url: str) -> int | Literal["short_link"] | None:
    parsed = urlparse(url)
    if not _is_wb_host(parsed.netloc):
        return None
    host = parsed.netloc.lower()
    if host == "go.wb.ru" or host.endswith(".go.wb.ru"):
        return "short_link"
    match = _CATALOG_PATH_RE.match(parsed.path)
    if match:
        return int(match.group(1))
    for value in parse_qs(parsed.query).get("nm", []):
        if re.fullmatch(r"\d{6,10}", value):
            return int(value)
    return None


def extract_sku(text: str) -> SkuParse:
    urls = _URL_RE.findall(text)
    short_link_seen = False
    for url in urls:
        found = _nm_from_wb_url(url)
        if found == "short_link":
            short_link_seen = True
        elif isinstance(found, int):
            return SkuParse(nm_id=found, reason=None)

    without_urls = _URL_RE.sub(" ", text)
    tokens = {int(t) for t in _SKU_TOKEN_RE.findall(without_urls)}
    if len(tokens) == 1:
        return SkuParse(nm_id=tokens.pop(), reason=None)
    if len(tokens) > 1:
        return SkuParse(nm_id=None, reason="ambiguous")
    if short_link_seen:
        return SkuParse(nm_id=None, reason="short_link")
    return SkuParse(nm_id=None, reason="not_found")
