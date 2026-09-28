import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings

CONFIG_ENV_KEYS = (
    "BOT_TOKEN",
    "DATABASE_PATH",
    "THRESHOLD_PCT",
    "MAX_ACTIVE_TRACKINGS",
    "UNAVAILABLE_WINDOW_HOURS",
    "CYCLE_MIN_SLEEP_MINUTES",
    "CYCLE_MAX_SLEEP_MINUTES",
    "DEFAULT_DEST",
    "REGIONS",
)


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Изолирует тест от реального .env и переменных окружения."""
    for key in CONFIG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def make_settings(**overrides):
    return Settings(_env_file=None, **overrides)


def test_defaults_when_only_required_fields(clean_env):
    settings = make_settings(bot_token="123:secret", default_dest="Курск")
    assert settings.database_path == "data/price_radar.db"
    assert settings.threshold_pct == pytest.approx(0.5)
    assert settings.max_active_trackings == 100
    assert settings.unavailable_window_hours == 48
    assert settings.cycle_min_sleep_minutes == 45
    assert settings.cycle_max_sleep_minutes == 75


def test_bot_token_is_secret_str(clean_env):
    settings = make_settings(bot_token="123:secret", default_dest="Курск")
    assert settings.bot_token.get_secret_value() == "123:secret"
    assert "123:secret" not in repr(settings.bot_token)


def test_missing_bot_token_raises(clean_env):
    with pytest.raises(ValidationError):
        make_settings(default_dest="Курск")


def test_override_from_environment(clean_env):
    import os

    os.environ["BOT_TOKEN"] = "999:tok"
    os.environ["DEFAULT_DEST"] = "Москва"
    os.environ["THRESHOLD_PCT"] = "1.5"
    os.environ["MAX_ACTIVE_TRACKINGS"] = "50"
    os.environ["CYCLE_MIN_SLEEP_MINUTES"] = "10"
    os.environ["CYCLE_MAX_SLEEP_MINUTES"] = "20"
    settings = Settings(_env_file=None)
    assert settings.threshold_pct == pytest.approx(1.5)
    assert settings.max_active_trackings == 50
    assert settings.default_dest == "Москва"


def test_regions_parsed_from_env_json(clean_env):
    import json
    import os

    os.environ["BOT_TOKEN"] = "1:x"
    os.environ["DEFAULT_DEST"] = "Курск"
    os.environ["REGIONS"] = json.dumps({"Курск": "-1000000", "Москва": "-1000100"})
    settings = Settings(_env_file=None)
    assert settings.regions == {"Курск": "-1000000", "Москва": "-1000100"}


def test_cycle_min_must_be_positive(clean_env):
    with pytest.raises(ValidationError):
        make_settings(
            bot_token="1:x",
            default_dest="Курск",
            cycle_min_sleep_minutes=0,
            cycle_max_sleep_minutes=75,
        )


def test_cycle_max_must_be_greater_than_min(clean_env):
    with pytest.raises(ValidationError):
        make_settings(
            bot_token="1:x",
            default_dest="Курск",
            cycle_min_sleep_minutes=75,
            cycle_max_sleep_minutes=75,
        )


def test_threshold_must_be_positive(clean_env):
    with pytest.raises(ValidationError):
        make_settings(bot_token="1:x", default_dest="Курск", threshold_pct=0)


def test_load_settings_reads_env_file(clean_env):
    env_path = clean_env / ".env"
    env_path.write_text(
        "BOT_TOKEN=7:abc\n"
        "DEFAULT_DEST=Курск\n"
        "THRESHOLD_PCT=2.0\n",
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.bot_token.get_secret_value() == "7:abc"
    assert settings.default_dest == "Курск"
    assert settings.threshold_pct == pytest.approx(2.0)
