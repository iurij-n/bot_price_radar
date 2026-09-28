# 02: Парсинг Артикула (extract_sku) и адаптер Wildberries (MarketplaceClient)

**What to build:** Пользовательское сообщение (числовой Артикул или ссылка карточки) детерминированно превращается в Артикул либо в явный вердикт отказа; шов `MarketplaceClient` с первым адаптером WBClient по заданному Региону (dest) возвращает карточки Товаров с Финальной ценой в копейках. Поведение парсинга и инварианты шва зафиксированы в `docs/architecture.md` §2.1 и §2.3, факты WB API — в `docs/research/wb-anon-pricing.md` и `docs/research/wb-regions.md` (пересказ не нужен). Ключевое различие: «нет карточки» (missing) vs «есть карточка, нет цены» = Недоступный товар.

**Blocked by:** 01 (каркас проекта)

**Status:** done

- [x] `extract_sku`: чистое 6–10-значное число → Артикул; `/catalog/<nm>`; `/product-card/<nm>`; `detail.aspx?nm=<digits>` (параметр где угодно в query); `go.wb.ru/...` → отказ short_link; несколько разных чисел → ambiguous; несколько одинаковых → Артикул; мусор → not_found; 5 и 11 цифр → не Артикул
- [x] Шов `MarketplaceClient` (Protocol + CardSnapshot/BatchResult) по `docs/architecture.md` §2.1; адаптер WBClient использует `cards/v4`, `marketplace = "wb"`
- [x] dest обязателен: пустой/невалидный dest → `MarketplaceError`, а не пустой результат
- [x] Батчи ≤1000 артикулов на запрос, разбивка скрыта внутри адаптера; цены — только Финальная цена (`sizes[].price.product`) в целых копейках; сверка по id, не по длине
- [x] Сетевые ошибки, HTTP 4xx/5xx, битый JSON → `MarketplaceError`; ретраев шов не делает
- [x] Unit-тесты `extract_sku` по всем перечисленным случаям (spec, «Решения по тестированию» п.1)
- [x] Тесты WBClient на JSON-фикстурах + `httpx.MockTransport` без сети: 2500 Артикулов → ровно 3 запроса 1000/1000/500, покрыты все; пустой dest → `MarketplaceError`; нет цены → Недоступный товар; nm вне ответа → missing (spec, п.2)
- [x] Демо-скрипт получения карточки по живому Артикулу помечен как manual/опциональный (не часть автоматического прогона)

## Comments

**Реализация (TDD red-green-refactor):**
- `src/app/domain/parsing.py` — `SkuParse`/`extract_sku`, чистая (тест через AST проверяет отсутствие импортов httpx/aiogram/sqlalchemy).
- `src/app/marketplaces/base.py` — Protocol `MarketplaceClient` + frozen-значения `CardSnapshot`/`BatchResult` + `MarketplaceError`; про WB ничего не знает.
- `src/app/marketplaces/wb.py` — `WBClient`: httpx.AsyncClient инъектируется конструктором (экземпляр или factory; по умолчанию создаётся сам с таймаутом 15с), заголовок `User-Agent` (Chrome-подобный) добавляется на каждый запрос; `cards/v4/detail` с `appType=1&curr=rub&locale=ru&spp=30`, `nm` через `;`; резка >1000 внутри; сверка по `products[].id`.
- Тесты: `tests/unit/test_parsing.py` (17), `tests/marketplaces/test_wb_client.py` (18) + фикстуры `card_normal/card_unavailable/partial/empty.json`; сеть — только `httpx.MockTransport`.

**Тест-команда:** `.venv\Scripts\python.exe -m pytest -q` → `48 passed in ~6s` (весь набор, включая каркас тикета 01).

**Принятые решения (не противоречат §2.1/§2.3):**
- Финальная цена при нескольких sizes с разными `price.product` — минимум (цена, которую реально увидит покупатель); формат «несколько разных цен в одном nm» архитектурой не фиксирован.
- `photo_url` — относящая ссылка из `coverImage` ответа (если поля нет — `None`); полная сборка через basket-хосты — вне шва, как и предписано (§2.1: «полная собирается в Adapter», здесь — pass-through относительной).
- Дубликаты nm во входах схлопываются; инвариант `set(cards.nm) ∪ missing == set(items)` соблюдён.

**Демо-скрипт по живому Артикулу:** помечен manual/опциональный — сознательно НЕ добавлен в автосток (тесты не ходят в сеть; живой прогон — ручная проверка пользователя по команде из README-заметки выше, риск x-pow/429).
