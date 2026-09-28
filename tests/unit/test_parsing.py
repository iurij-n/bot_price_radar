import ast
import pathlib

import pytest

from app.domain.parsing import SkuParse, extract_sku


def test_clean_six_digit_number_is_parsed():
    assert extract_sku("123456") == SkuParse(nm_id=123456, reason=None)


def test_clean_ten_digit_number_is_parsed():
    assert extract_sku("1280469586") == SkuParse(nm_id=1280469586, reason=None)


def test_five_digits_is_not_a_sku():
    assert extract_sku("12345") == SkuParse(nm_id=None, reason="not_found")


def test_eleven_digits_is_not_a_sku():
    assert extract_sku("12345678901") == SkuParse(nm_id=None, reason="not_found")


def test_trash_text_is_not_found():
    assert extract_sku("привет, как дела?") == SkuParse(nm_id=None, reason="not_found")


def test_empty_string_is_not_found():
    assert extract_sku("") == SkuParse(nm_id=None, reason="not_found")


def test_number_glued_to_letters_is_not_a_sku():
    assert extract_sku("abc123456") == SkuParse(nm_id=None, reason="not_found")
    assert extract_sku("123456abc") == SkuParse(nm_id=None, reason="not_found")


def test_catalog_url_is_parsed():
    text = "https://www.wildberries.ru/catalog/123456789/detail.aspx"
    assert extract_sku(text) == SkuParse(nm_id=123456789, reason=None)


def test_product_card_url_is_parsed():
    text = "https://wb.ru/product-card/123456789"
    assert extract_sku(text) == SkuParse(nm_id=123456789, reason=None)


def test_detail_aspx_nm_param_is_parsed():
    text = "https://www.wildberries.ru/catalog/detail.aspx?nm=987654321"
    assert extract_sku(text) == SkuParse(nm_id=987654321, reason=None)


def test_nm_param_not_first_in_query_is_parsed():
    text = "https://www.wildberries.ru/catalog/detail.aspx?query=x&supplier=a&nm=987654321&size=28"
    assert extract_sku(text) == SkuParse(nm_id=987654321, reason=None)


def test_short_link_is_rejected():
    assert extract_sku("https://go.wb.ru/abc123") == SkuParse(
        nm_id=None, reason="short_link"
    )


def test_two_equal_numbers_yield_sku():
    assert extract_sku("123456 123456") == SkuParse(nm_id=123456, reason=None)


def test_two_different_numbers_are_ambiguous():
    assert extract_sku("123456 654321") == SkuParse(nm_id=None, reason="ambiguous")


def test_three_numbers_with_duplicates_still_ambiguous_when_different():
    assert extract_sku("123456 123456 999999") == SkuParse(
        nm_id=None, reason="ambiguous"
    )


def test_url_takes_precedence_over_other_numbers():
    text = "смотри https://www.wildberries.ru/catalog/123456789/detail.aspx и ещё 555555"
    parse = extract_sku(text)
    assert parse == SkuParse(nm_id=123456789, reason=None)


def test_domain_parsing_has_no_forbidden_imports():
    source = pathlib.Path(__file__).resolve().parents[2] / "src" / "app" / "domain" / "parsing.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    assert imported.isdisjoint({"httpx", "aiogram", "sqlalchemy"})
