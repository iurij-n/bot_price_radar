# -*- coding: utf-8 -*-
"""Unit-тесты domain.decide (spec, п.1): порог включительно, None-операнды,
защита нулевой базы; чистая функция — без I/O и фейков."""

from __future__ import annotations

import pytest

from app.domain.decisions import NotificationDecision, decide


def test_exact_threshold_down_is_inclusive():
    d = decide(100_000, 99_500, 0.5)
    assert d is not None
    assert d.direction == "down"
    assert d.delta_pct == pytest.approx(-0.5)


def test_exact_threshold_up_is_inclusive():
    d = decide(100_000, 100_500, 0.5)
    assert d is not None
    assert d.direction == "up"
    assert d.delta_pct == pytest.approx(0.5)


def test_exact_threshold_on_awkward_base():
    # 0.5% от 99 000 копеек = ровно 495 — граница включительно
    d = decide(99_000, 98_505, 0.5)
    assert d is not None
    assert d.delta_pct == pytest.approx(-0.5)


def test_below_threshold_up_returns_none():
    assert decide(100_000, 100_499, 0.5) is None


def test_below_threshold_down_returns_none():
    assert decide(100_000, 99_501, 0.5) is None


def test_above_threshold_down():
    d = decide(1_000_000, 994_000, 0.5)
    assert d == NotificationDecision(direction="down", delta_pct=pytest.approx(-0.6))


def test_above_threshold_up():
    d = decide(1_000_000, 1_012_000, 0.5)
    assert d is not None
    assert d.direction == "up"
    assert d.delta_pct == pytest.approx(1.2)


def test_comparison_none_first_measurement_returns_none():
    assert decide(None, 99_000, 0.5) is None


def test_current_none_unavailable_is_not_price_drop():
    assert decide(100_000, None, 0.5) is None


def test_both_none_returns_none():
    assert decide(None, None, 0.5) is None


def test_zero_comparison_guard_no_division_by_zero():
    assert decide(0, 10_000, 0.5) is None
    assert decide(0, 0, 0.5) is None


def test_negative_comparison_guard():
    assert decide(-100, 100, 0.5) is None


def test_no_change_below_positive_threshold_returns_none():
    assert decide(100_000, 100_000, 0.5) is None


def test_custom_threshold_boundary():
    assert decide(100_000, 101_000, 1.0) is not None
    assert decide(100_000, 100_999, 1.0) is None


def test_big_kopeck_values():
    d = decide(1_234_567_890, 1_228_000_000, 0.5)
    assert d is not None
    assert d.direction == "down"
