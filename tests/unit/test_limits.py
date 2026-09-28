from app.domain.limits import can_add


def test_can_add_zero_active():
    assert can_add(0, 100) is True


def test_can_add_just_below_limit():
    assert can_add(99, 100) is True


def test_can_add_at_limit_is_refused():
    assert can_add(100, 100) is False


def test_can_add_above_limit_is_refused():
    assert can_add(101, 100) is False


def test_can_add_smaller_limit_boundary():
    assert can_add(0, 1) is True
    assert can_add(1, 1) is False
