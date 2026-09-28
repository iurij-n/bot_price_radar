# Price Radar

Telegram-бот отслеживания финальных цен маркетплейсов (первый — Wildberries).
Стек: Python 3.12, aiogram 3, SQLAlchemy async + aiosqlite (SQLite), httpx,
pydantic-settings, pytest + pytest-asyncio (asyncio_mode=auto), Docker.

## Запуск локально

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item .env.example .env  # заполнить BOT_TOKEN и dest-ID регионов
.venv\Scripts\python.exe -m app.main
```

## Запуск в Docker

```powershell
docker compose up --build   # Ctrl+C / SIGTERM — корректное завершение
```

## Тесты

```powershell
.venv\Scripts\python.exe -m pytest -q
```

## Документация

- Архитектура и структура пакетов: `docs/architecture.md`
- Доменный словарь: `CONTEXT.md`
- Спека и тикеты: `.scratch/wb-price-radar/`

## Деплой на VPS

Требования:
- Docker + Docker Compose на сервере.
- **Резидентный российский IP** — WB отдаёт региональные цены и режет
  датацентр-запросы (см. `docs/research/wb-regions.md`).

Шаги:

```bash
git clone <repo> && cd bot_price_radar
cp .env.example .env && nano .env   # BOT_TOKEN от @BotFather; REGIONS — dest-ID
                                    # собрать ВРУЧНУЮ из браузера, рецепт:
                                    # docs/research/wb-regions.md §2 — НЕ ЗАБЫТЬ
mkdir -p data backups
docker compose up -d --build        # приложение + контейнер бэкапа (цикл раз в сутки)
docker compose logs -f app          # проверить, что цикл стартовал без ошибок
```

Бэкап (можно вручную в любой момент; ротация — последние 7 снимков в `backups/`):

```bash
docker compose run --rm backup python scripts/backup_db.py
```

Восстановление из бэкапа:

```bash
docker compose down
cp backups/price_radar-<штамп>.db data/price_radar.db
docker compose up -d
```

## Локальная отладка

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
```
