"""Конфигурация приложения: pydantic-settings, чтение .env.

Поля соответствуют docs/architecture.md §1 (config.py): токен бота,
Порог уведомления, лимит активных Отслеживаний, окно Недоступного товара,
диапазон сна Цикла проверки, справочник Регионов (имя -> dest) и dest
по умолчанию (Курск).
"""

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: SecretStr
    database_path: str = "data/price_radar.db"
    threshold_pct: float = 0.5
    max_active_trackings: int = 100
    unavailable_window_hours: int = 48
    cycle_min_sleep_minutes: int = 45
    cycle_max_sleep_minutes: int = 75
    default_dest: str
    regions: dict[str, str] = {}

    @field_validator("cycle_min_sleep_minutes", "unavailable_window_hours", "max_active_trackings")
    @classmethod
    def _positive_ints(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("должно быть больше 0")
        return value

    @field_validator("threshold_pct")
    @classmethod
    def _threshold_positive(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Порог уведомления должен быть больше 0")
        return value

    @model_validator(mode="after")
    def _cycle_range_valid(self) -> "Settings":
        if self.cycle_min_sleep_minutes <= 0:
            raise ValueError("cycle_min_sleep_minutes должен быть больше 0")
        if self.cycle_max_sleep_minutes <= self.cycle_min_sleep_minutes:
            raise ValueError(
                "cycle_max_sleep_minutes должен быть больше cycle_min_sleep_minutes"
            )
        return self


def load_settings() -> Settings:
    """Загружает конфигурацию из окружения и .env в текущей директории."""
    return Settings()
