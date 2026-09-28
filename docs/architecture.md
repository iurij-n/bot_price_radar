# Архитектура Price Radar (план, не код)

Документ фиксирует архитектуру приложения до реализации. Терминология — строго из
словаря deep-module: **Module / Interface / Implementation / Seam / Adapter / Depth /
Leverage / Locality**. Доменные термины — из `CONTEXT.md` (Товар, Артикул, Регион
(dest), Финальная цена, Цена сравнения, Порог уведомления, Отслеживание, Цикл
проверки, Уведомление, Недоступный товар, Неактивный пользователь).

Обоснования зафиксированы в ADR: ADR-0001 (анонимный фронтенд-API WB), ADR-0002
(SQLite + один процесс, два asyncio-задания), ADR-0003 (разделение
product / product_price / subscription), ADR-0004 (шов MarketplaceClient). Факты
WB API — в `docs/research/wb-anon-pricing.md` и `docs/research/wb-regions.md`.

Стек (зафиксирован дизайном-контрактом): Python 3.12, aiogram 3, SQLAlchemy async +
aiosqlite, httpx, pydantic-settings (.env), Docker — один контейнер, два asyncio-задания
в одном процессе: polling-бот и воркер Цикла проверки. Тесты: pytest +
pytest-asyncio, фейки/моки асинхронные. Деньги везде — целые копейки (`int`).

---

## 1. Скелет каталогов

```
src/app/
  main.py                  — композиция: сборка Store/Client/Sender, запуск двух задач (polling + воркер), graceful shutdown
  config.py                — pydantic-settings: токен, DB_PATH, Порог уведомления, диапазон сна 45–75 мин, dest по умолчанию, лимит 100, таймауты
  domain/                  — чистое ядро: только функции над значениями, ноль I/O, ноль импортов aiogram/SQLAlchemy/httpx
    decisions.py           — decide(): пересечение Порога уведомления → NotificationDecision | None
    availability.py        — политика «48ч Недоступный товар + одиночное уведомление»
    region_change.py       — политика «смена Региона = сброс Цены сравнения» (ADR-0003)
    limits.py              — политика лимита активных отслеживаний (can_add)
    parsing.py             — extract_sku(): Артикул из текста/ссылки (чистая функция)
  marketplaces/            — шов внешнего мира цен
    base.py                 — Interface MarketplaceClient + значения CardSnapshot/BatchResult (Protocol)
    wb.py                   — Adapter WBClient: httpx, cards/v4, разбивка на батчи ≤1000, парсинг sizes[].price
  store/                   — шов персистентности, Interface на языке домена
    db.py                   — engine/session-фабрика (aiosqlite), создание схемы при старте
    models.py               — SQLAlchemy-таблицы: users, products, product_prices, subscriptions, price_history
    repository.py           — Module Store: прячет сессии/транзакции; методы add_tracking, apply_batch, ...
  notify/                  — шов доставки сообщений (отдельно от bot, чтобы worker не импортировал handlers)
    interface.py            — Interface NotifySender: send(OutgoingMessage) -> DeliveryResult (Protocol + значения)
    telegram.py             — Adapter aiogram-обёртка: троттлинг 1 msg/s, FloodWait-ретраи, Forbidden → UserBlocked
  bot/                     — тонкий UI-слой, бизнес-логики нет
    handlers.py             — /start /help /list /region, добавление сообщением, удаление 🗑 (парсинг → domain/store → формат)
    keyboards.py            — inline-клавиатуры: подтверждение добавления, кнопка 🗑
    messages.py             — тексты и форматирование (цены из копеек, «Товар не найден», дубль-сообщение)
    confirm.py              — state-machine короткого inline-подтверждения «артикул → карточка → да/нет»
tests/
  unit/                    — domain/: decide, availability, region_change, limits, parsing — без I/O вообще
  store/                   — Store поверх SQLite :memory: (aiosqlite): интеграционные проверки инвариантов
  marketplaces/            — WBClient на сохранённых JSON-фикстурах + httpx.MockTransport (сети нет)
  notify/                  — NotifySender на фейковом api (троттлинг, FloodWait, Forbidden)
  bot/                     — handlers на fake Store + fake NotifySender (тонкость слоя)
  e2e/                     — полный Цикл проверки: fake MarketplaceClient + fake Store + fake NotifySender
```

Правило зависимостей: `domain` не импортирует ничего из `app`; `bot`/`workers`
импортируют `domain`, `store`, `notify` и `marketplaces` только через их Interface
(`base.py`, `interface.py`); конкретные Adapter (`wb.py`, `telegram.py`, `models.py`)
знает только `main.py` (композиция) и собственные тесты.

---

## 2. Карта модулей и швов

Швы (Seam) в системе ровно четыре — все на границах с внешним миром или с
необратимыми решениями: **MarketplaceClient**, **Store**, **NotifySender**,
и внутренний чистый шов **domain-функций** (шов «логика против остального»).
Всё остальное — Implementation за этими швами.

### 2.1 Seam: MarketplaceClient (`marketplaces/base.py`)

Единственная точка, через которую в систему попадают цены (ADR-0004). В рантайме
её вызывает только воркер Цикла проверки; bot использует тот же шов однократно —
получить карточку при добавлении отслеживания.

Interface (весь):

```python
class MarketplaceClient(Protocol):
    marketplace: str  # стабильный ключ для колонки products.marketplace: "wb"

    async def fetch_cards_batch(
        self, items: Sequence[int], dest: str
    ) -> BatchResult: ...
```

Возвращаемые значения (immutable dataclass-ы, часть Interface'а):

```python
@dataclass(frozen=True)
class CardSnapshot:
    nm_id: int
    name: str | None
    photo_url: str | None      # относительная ссылка WB; полная собирается кэшем строк в Adapter
    link: str
    final_price_kop: int | None   # None == Недоступный товар в этом dest (нет price в sizes[])
    available: bool               # totalQuantity > 0 и цена есть

@dataclass(frozen=True)
class BatchResult:
    cards: tuple[CardSnapshot, ...]
    missing: frozenset[int]       # запрошенные nm, отсутствующие в ответе
```

Инварианты Interface'а:
- Можно передать **любое** число nm (хоть 5000): Adapter сам режет на батчи ≤1000 —
  лимит cards/v4 (research §3) спрятан внутри Implementation, caller'ы о нём не знают.
- `dest` обязателен и не может быть пустым: без dest WB отдаёт пустой список,
  что неотличимо от «нет карточек» (research wb-regions §4). Пустой/невалидный dest → ошибка, а не пустой результат.
- Результат всегда полный: `set(cards.nm) ∪ missing == set(items)`; сверка по
  `products[].id`, никогда по длине (research §3).
- Цены — только Финальная цена (`sizes[].price.product`) в копейках; Базовая цена
  через шов не проходит (не объект отслеживания, CONTEXT).

Ошибки: `MarketplaceError` (сеть/таймаут/HTTP 4xx-5xx/битый JSON/невалидный dest).
Partial-failure внутри батча не моделируется: один батч — одна ошибка.
Повторных попыток шов не делает — это политика caller'а (см. 2.6).

Depth: высокая — за двумя методами и четырьмя полями спрятаны URL-склейка
`nm=';'.join`, обязательные параметры `appType=1&curr=rub&locale=ru&spp=30`,
разбивка батчей, разбор `sizes[].price`, различение «нет цены» vs «нет карточки»
(research wb-anon-pricing §1.2, wb-regions §4).
Adapters: `WBClient` (httpx) — **пока единственный**. Шов сознательно
предварительный (один Adapter = гипотетический шов по правилу глубины); он
оправдан планом подключения Ozon и колонкой `marketplace` в уникальных ключах с
первого дня (ADR-0004) и зафиксирован до написания WB-кода, чтобы артикулы
площадок не столкнулись в одном пространстве идентификаторов.
`marketplace: str` — второй элемент Interface'а и ровно тот, что нужен Store, чтобы
не знать, какую площадку он опрашивает.

### 2.2 Seam: Store (`store/repository.py`)

Module поверх SQLAlchemy; Interface говорит на языке домена, Implementation
(таблицы, сессии, транзакции, `async with`, flush) не видна наружу полностью:
только на 5 таблиц, зафиксированных ADR-0003 и дизайн-контрактом:
`users(telegram_id, dest, status)`, `products(marketplace, nm_id — unique-пара,
name, photo_url, link, first_seen)`, `product_prices(product_id, dest — unique-пара,
final_price_kop, availability, last_checked_at, unavailable_since)`,
`subscriptions(user_id, product_id — unique-пара, last_notified_price_kop, status,
unavailable_notified)`, `price_history(product_id, dest, price_kop, recorded_at)`.

> Примечание к схеме: флаг однократной отправки уведомления о Недоступном товаре
> живёт в `subscriptions.unavailable_notified` — недоступность это состояние
> пары товар×dest, но «уведомить один раз» — свойство каждого Отслеживания; сброс
> флага — при возвращении цены.

Interface (сгруппирован по слоям вызывающих, но это один Module):

```python
class Store(Protocol):
    # --- пользователи / UI ---
    async def ensure_user(self, telegram_id: int, dest: str) -> None
    async def get_user_dest(self, telegram_id: int) -> str | None
        # read для bot-слоя: текущий Регион пользователя (тикеты 05/06)
    async def set_region(self, telegram_id: int, dest: str, prices: dict[int, int | None]) -> None
        # применяет политику region_change: новая Цена сравнения = текущая Финальная цена в новом dest (или None)
    async def upsert_card(self, marketplace: str, snapshot: CardSnapshot) -> int  # -> product_id
    async def add_tracking(self, telegram_id: int, product_id: int, base_price_kop: int | None) -> None
    async def remove_tracking(self, telegram_id: int, product_id: int) -> None
        # в конце чистит price_history пар product×dest, если активных Отслеживаний не осталось
    async def active_tracking_count(self, telegram_id: int) -> int
    async def list_trackings(self, telegram_id: int) -> tuple[TrackingRow, ...]
    async def find_tracking(self, telegram_id: int, product_id: int) -> TrackingRow | None

    # --- Цикл проверки (воркер) ---
    async def active_dests(self) -> frozenset[str]          # distinct dest активных Отслеживаний активных пользователей
    async def tracked_nms(self, dest: str) -> tuple[int, ...]
    async def apply_batch(self, dest: str, result: BatchResult, now: datetime) -> CycleDiff
        # upsert product_prices; ведёт unavailable_since; считает transition-и; пишет price_history
    async def notification_candidates(self, dest: str, diff: CycleDiff) -> tuple[NotifyCandidate, ...]
        # активные подписки на изменённые пары: (subscription_id, telegram_id, Цена сравнения, new_price_kop)
    async def unavailable_due(self, now: datetime, window: timedelta) -> tuple[UnavailableCandidate, ...]
        # пары с unavailable_since older than window и unavailable_notified == False
    async def commit_notification(self, subscription_id: int, new_comparison_kop: int | None) -> None
        # обновляет last_notified_price_kop (Цену сравнения) и/или unavailable_notified
    async def deactivate_user(self, telegram_id: int) -> None
        # Неактивный пользователь + все его Отслеживания → status=inactive (CONTEXT: не удалять)
    async def reactivate_user(self, telegram_id: int) -> None  # на /start после разблокировки
```

Инварианты Interface'а:
- Каждый публичный метод = одна атомарная транзакция; сессия/`commit`/`rollback`
  наружу не отдаются никогда.
- `apply_batch` идемпотентен по `now`: повторный вызов с тем же snapshot не
  создаёт второй записью history «на ту же минуту» дублей уведомлений не порождает
  (CycleDiff пустой, если цены не изменились).
- `price_history` чистится только когда на пару `product×dest` не осталось
  **никаких** записей подписок, кроме удалённых (дизайн-контракт) — удаление
  последнего Отслеживания запускает prune внутри `remove_tracking`.
- `set_region` молча сбрасывает Цену сравнения (ADR-0003), цены товаров не трогает.
- Методы ничего не знают про Telegram-UI и про WB-формат: параметром принимают
  только доменные значения (`CardSnapshot`, `CycleDiff`, int-копейки, str-dest).

Depth: за 16 методами спрятаны все SQL, join-ы (tracking rows: product + price +
subscription), upsert-логика unique-пар, каскады статусов, prune-истории, порядок
commit'а после доставки. Locality: схема меняется только в `models.py` +
`repository.py`, ни один caller не замечает.
Adapter (Implementation): `repository.py` поверх SQLAlchemy/aiosqlite/SQLite
(ADR-0002). Тестовый Adapter: in-memory fake Store (словари словарей) — тот же
Interface (см. §4); «маленький Adapter с маленьким Implementation» допустим,
настоящий SQL покрыт отдельным слоем тестов поверх того же Seam.

### 2.3 Чистое доменное ядро (`domain/`)

Маленькие _deep_ модули из чистых функций: никаких сайд-эффектов, никаких
импортов Store/Telegram/WB, возвращают результаты, а не mutate'ят. Это seam
«решения против мира»: тестируется без БД и Telegram в один вызов.

`domain/decisions.py` — центральный модуль:

```python
@dataclass(frozen=True)
class NotificationDecision:
    direction: Literal["up", "down"]
    delta_pct: float

def decide(
    comparison_price_kop: int | None,
    current_price_kop: int | None,
    threshold_pct: float,          # Порог уведомления, default 0.5 — из config
) -> NotificationDecision | None: ...
```

Инварианты:
- `comparison_price_kop is None` → `None` (нет Цены сравнения — первый замер после
  добавления/смены Региона; база ставится при `commit` или `set_region`, не здесь).
- `current_price_kop is None` → `None` (Недоступный товар — не «падение цены»,
  это другой сигнал, см. availability).
- `|cur − cmp| / cmp >= threshold_pct / 100` → решение; сравнение дробное, но
  операнды int-копейки, float только для Δ% — порог сравнения фиксирован и входит
  в Interface, caller не может «случайно» поменять семантику.
- Функция ничего не знает о подписках: это чистый арифметический факт.

`domain/availability.py`:

```python
def should_notify_unavailable(
    unavailable_since: datetime | None,
    now: datetime,
    already_notified: bool,
    window: timedelta,            # 48ч — из config
) -> bool: ...
```
Политика: ровно одно уведомление, когда непрерывная недоступность ≥ window;
`already_notified=True` → всегда False (сброс флага — ответственность Store при
возвращении цены); `unavailable_since is None` → False.

`domain/region_change.py`:

```python
def reset_comparison(current_price_in_new_dest: int | None) -> int | None: ...
```
Политика ADR-0003: при смене dest Цена сравнения становится равной текущей
Финальной ценой в новом dest (`None`, если товар там пока Недоступный) — чтобы
следующее же уведомление считалось от новой базы и не слало ложных «падений».

`domain/limits.py`:

```python
def can_add(active_count: int, limit: int) -> bool: ...   # limit = 100 из config
```

`domain/parsing.py`:

```python
@dataclass(frozen=True)
class SkuParse:
    nm_id: int | None
    reason: Literal[None, "not_found", "ambiguous", "short_link"]
    # nm_id is not None ⇒ reason is None

def extract_sku(text: str) -> SkuParse: ...
```

Политика разбора (фиксируется здесь, handler'ы только форматируют `reason`):
1. URL на `wildberries.ru`/`wb.ru` → артикул из пути `/catalog/<digits>` или
   `/product-card/<digits>`, либо из query `detail.aspx?nm=<digits>` (формат
   `nm=NNNN`; параметр может идти не первым).
2. Короткая ссылка `go.wb.ru/<код>` → `short_link`: артикул из неё не извлекается
   (нужен redirect-резолвер, сознательно не строим) — просить артикул или полную ссылку.
3. Иначе — числовые токены длиной 6–10 цифр (для WB, CONTEXT «Артикул»). Все
   токены равны → `nm_id`; разные → `ambiguous` («старшее число» НЕ угадываем:
   ложный артикул дороже ложного отказа).
4. Ничего → `not_found` → UX-текст «Товар не найден».

Depth: 4 функции прячут весь «когда слать, а когда нет» — воркер и handler'ы
делают только I/O-последовательности, спорные решения проверяются unit-тестами
без фейков вообще.

### 2.4 Seam: NotifySender (`notify/interface.py`)

Единственный способ отправить сообщение пользователю. Живёт отдельно от `bot/`,
чтобы воркер не импортировал handlers (направление зависимостей: worker → notify).

Interface (весь):

```python
@dataclass(frozen=True)
class OutgoingMessage:
    chat_id: int
    text: str
    button: tuple[str, str] | None    # (label="Открыть товар", url)
    callback: tuple[str, str] | None = None  # (label="🗑 Удалить", callback_data) — доп. строка, /list

class DeliveryResult:  # frozen-иерархия
    ...
    # Delivered | UserBlocked | Failed(exc_summary: str, attempt_count: int)

class NotifySender(Protocol):
    async def send(self, message: OutgoingMessage) -> DeliveryResult: ...
```

Инварианты Interface'а:
- Никогда не бросает на ошибки доставки — только `DeliveryResult` (принцип
    «возвращай результат, не производь сайд-эффект»; caller решает сам, что делать).
- `UserBlocked` = TelegramForbidden (пользователь заблокировал бота) — это
    **сигнал**, а не действие: деактивацию выполняет caller через
    `Store.deactivate_user`, швы Notify и Store не знают друг про друга.
- Глобальный троттлинг ≥ 1 msg/s между любыми sendMessage — это свойство
    Implementation, а не caller'ов; `/list` с сотней сообщений и пачка
    Уведомлений из воркера проходят через один и тот же модуль и не конфликтуют.
- FloodWait ретраится внутри с уважением к `retry_after` aiogram, с верхним
    лимитом попыток (по умолчанию 3) → после лимита `Failed`, не зависание.
- Экземпляр один на процесс (композиция в `main.py`), разделения bot/worker нет —
    иначе троттлинг разъехался бы.

Depth: за методом `send` спрятаны inline-клавиатура кнопки «Открыть товар»,
formatting-минimum (текст caller собирает сам), rate-limit-цирк, ретраи, матчинг
типов исключений aiogram. Locality: поведение при флудах Telegram чинится в одном файле.
Adapters: `notify/telegram.py` (aiogram Bot). Тестовый Adapter — fake,
записывающий сообщения в список и умеющий инжектить Forbidden/FloodWait.

### 2.5 Bot handlers (`bot/handlers.py`) — тонкий слой UI

Правило-инвариант всего слоя: **handler = чтение ввода → extract_sku/парсинг
команды → вызов domain-функции и/или Store → форматирование ответа NotifySender.**
Запрещено: SQL, httpx, бизнес-решения (пороги, 48ч, сброс базы) внутри handler'а.

Команды и вызовы:
- `/start` → `Store.ensure_user` + `reactivate_user`, приветствие, подсказка про `/region`.
- `/help` → `bot/messages.py`.
- `/region` → inline-клавиатура Regions-справочника (см. §7, открытый вопрос) → `Store.set_region` (политика из `domain/region_change.py`).
- текстовое сообщение (артикул/ссылка) → `extract_sku` → `MarketplaceClient.fetch_cards_batch([nm], user_dest)` →
  пусто в `missing` → «Товар не найден» (в дубль-сообщение с подсказкой);
  `can_add` из `domain/limits.py` → лимит → иначе inline-подтверждение (`bot/confirm.py`:
  callback-кнопка «Добавить» хранит product_id+nms в callback-данных, без глобального
  state) → «да» → `upsert_card` + `add_tracking(base = snapshot.final_price_kop)`.
- `/list` → `Store.list_trackings` → по одному сообщению на товар, `NotifySender.send`
 (троттлинг уже внутри шва), кнопка 🗑 на каждом → callback → `Store.remove_tracking`.
 Реализация (тикет 06): текстовые строки «название / Артикул / Финальная цена» —
 фото через NotifySender не передаётся (шов умеет только текст+кнопки);
 `UserBlocked` → `deactivate_user` + остановка выдачи, `Failed` → лог + продолжение;
 удаление известной и неизвестной пары даёт один и тот же ответ «Удалено».

- Дубль отслеживания (`find_tracking` != None) → отдельное сообщение-подсказка (UX-контракт).

Depth слоя сознательно низкая — это и есть цель: shallow модуль поверх глубоких
(Store/NotifySender/domain). Тесты handlers — только проверка «правильный вызов +
правильный текст», без бизнес-веток.

### 2.6 Worker: Цикл проверки (`workers/cycle.py`)

Оркестратор и **единственная точка рантайма, вызывающая MarketplaceClient**.
Принимает зависимости, не создаёт их (тестируется подсовыванием фейков):

```python
async def run_check_loop(
    store: Store,
    client: MarketplaceClient,
    sender: NotifySender,
    cfg: Settings,
    sleep_fn=random_sleep_45_75,      # инъектируемый сон (тесты: 0)
) -> None: ...
```

Шаги одной итерации (цикл = `await sleep_fn()` → …):
1. `dests = store.active_dests()`.
2. Для каждого dest: `nms = store.tracked_nms(dest)` → `client.fetch_cards_batch(nms, dest)`
   (разбивку ≤1000 делает Adapter, не воркер).
   Ошибка `MarketplaceError` на регион → **лог + пропуск региона в этом цикле**, без
   ретраев и без остановки других регионов (старые `product_prices` не трогаются;
   следующий цикл через ~45–75 мин).
3. `diff = store.apply_batch(dest, result, now)` — обновление Финальных цен,
   `unavailable_since`, transition-и, запись price_history.
4. `candidates = store.notification_candidates(dest, diff)`; для каждого —
   `decide(cmp, cur, cfg.threshold_pct)` (чистая функция); решение → `sender.send(...)`
   (текст + кнопка «Открыть товар») → при `Delivered`: `store.commit_notification(id, cur)`;
   при `UserBlocked`: `store.deactivate_user(telegram_id)` (CONTEXT: Неактивный
   пользователь, отслеживания не удалять); при `Failed`: ничего не коммитим —
   Цена сравнения не сдвинется, в следующем цикле попытка повторится сама.
5. Недоступные пары: `store.unavailable_due(now, cfg.unavailable_window)` +
   `should_notify_unavailable(...)` → тот же путь доставки/commit (ставит
   `unavailable_notified`).
6. `store.prune_history()` — вычистить историю пар без подписок.

Воркер ничего не знает ни о SQL, ни о JSON WB, ни о Telegram-исключениях —
только три шва. Это делает его почти тривиальным для E2E-теста (§4).

---

## 3. Данные: кто чем владеет (сводка к ADR-0003)

| Сущность (CONTEXT) | Таблица | Владелец решения |
|---|---|---|
| Товар (marketplace+Артикул) | `products` | Store |
| Регион × Финальная цена / Недоступность | `product_prices` (unique product×dest) | apply_batch |
| Отслеживание + Цена сравнения | `subscriptions` (unique user×product) | add_tracking / commit_notification |
| История Финальных цен | `price_history` | apply_batch + prune |
| Регион пользователя, статус | `users` | ensure_user / set_region / deactivate_user |

Два разных «предыдущих значения» не путать: diff для price_history считает по
`product_prices` (глобальная цена), а Порог уведомления (`decide`) — по личной
Цене сравнения подписки (`last_notified_price_kop`). Interface `CycleDiff`/
`NotifyCandidate` фиксирует это разделение типами.

---

## 4. Поток данных (ASCII)

```
                     ┌─────────── Telegram ────────────┐
                     │                                 │
           сообщения/команды                  sendMessage (1 msg/s, FloodWait)
                     │                                 ▲
                     ▼                                 │
        ┌────────────────────┐                ┌────────┴────────┐
        │  aiogram polling   │                │  NotifySender   │  Seam: доставка
        │  (bot/handlers)    │                │  (aiogram-bot)  │  UserBlocked → деактивация
        └───┬────────────┬───┘                └────▲─────▲──────┘
            │            │ extract_sku             │     │
            ▼            ▼                         │     │
   ┌──────────────┐  ┌────────┐                    │     │
   │ Store (SQL)  │◄─┤ domain │  чистые решения:   │     │
   │  Seam: БД    │  │ decide │  decide/48ч/лимит/  │     │
   └──▲───▲───▲───┘  └───▲────┘  region-reset       │     │
      │   │   │          │                           │     │
   SQLite│   │      worker циклит:                    │     │
  (ADR-0002)                                         │     │
      └───┼──────────────────────────────────────────┘     │
          │  apply_batch → candidates → decide → send → commit
          │                                                │
  ┌───────┴────────┐   fetch_cards_batch(nms, dest)        │
  │ MarketplaceCli-│──────────────────────────┐            │
  │ ent Seam       │                          │            │
  │ (WBClient,     │              card.wb.ru/cards/v4/detail│
  │  батчи ≤1000)  │──────────────►  WB HTTP (ADR-0001) ───►┘ (ответ: prices/unavailable/missing)
  └────────────────┘
```

Текст-диаграмма последовательности Цикла проверки:

```
sleep(45–75м) → active_dests → per dest: tracked_nms → fetch_cards_batch → apply_batch
→ notification_candidates → decide → send → commit_notification / deactivate_user
→ unavailable_due → should_notify_unavailable → send → commit → prune_history
```

---

## 5. Тест-стратегия по швам

Принцип: интерфейс шва = тестовая поверхность (§13 правила глубины); tests
пересекают тот же Seam, что и callers.

1. **Unit (без I/O, без фейков)** — `tests/unit/`:
   - `decide`: порог ровно 0.5% (граница включительно), вверх/вниз, `None` база,
     `None` цена (Недоступный — не Уведомление о падении), ноль в базе (защита),
     копейки-переполнение мелочи.
   - `should_notify_unavailable`: 47:59 / ровно 48:00 / >48ч, already_notified,
     None since.
   - `extract_sku`: чистое число; ссылка `/catalog/123456789.html`; `detail.aspx?nm=`;
     `go.wb.ru/abc` → short_link; мусор; несколько разных чисел → ambiguous;
     одинаковые дубли → nm; 5 цифр / 11 цифр → не артикул.
   - `can_add` (0, 99, 100, 101), `reset_comparison`.
2. **WBClient** — `tests/marketplaces/`: сохранённые JSON-фикстуры (формат из
   research) + `httpx.MockTransport` (без сторонних mock-библиотек):
   разбор `sizes[].price.product`, отсутствующий `price` → `final_price_kop=None`,
   выбрасывание >1000-го id → корректная режущая логика (проверка: 2500 nm →
   3 запроса по 1000/1000/500, все id покрыты cards∪missing), пустой ответ без
   dest никогда не выходит за Adapter (валидация), 400 на невалидный dest →
   MarketplaceError.
3. **Store** — два уровня:
   - fake in-memory Store (тот же Interface) — для worker/bot тестов;
   - интеграционные `tests/store/` поверх `sqlite+aiosqlite:///:memory:`:
     unique-пары не дублируются, `remove_tracking` чистит history только при
     нуле подписок, `set_region` сбрасывает базу и не трогает `product_prices`,
     `deactivate_user` помечает tracking неактивными без удаления (CONTEXT).
4. **NotifySender** — fake-транспорт: троттлинг (10 сообщений → ≥9s в фейковом
   времени — инъектируемая clock), FloodWait → ретраи до лимита → Failed,
   Forbidden → UserBlocked (и ничего не бросает наружу).
5. **Worker** — `tests/bot/worker` fake Store + fake client + fake sender:
   порядок diff→decide→send→commit; `MarketplaceError` на одном dest не убивает
   остальные dest и весь цикл; `UserBlocked` рождает ровно один
   `deactivate_user`; Failed → commit НЕ вызван (Цена сравнения не сдвинута).
6. **E2E Цикла проверки** — `tests/e2e/`, pytest-asyncio: оба шва фейковые,
   реальный `domain`: сценарии «рост цены >0.5% → Уведомление + база сдвинута»,
   «изменение <порога → молчание, база не сдвинута», «товар недоступен 48ч →
   ровно одно уведомление, возврат цены → флаг сброшен», «TelegramForbidden →
   Неактивный пользователь, tracked_nms его больше не спрашивает», «добавление
   через bot-handler с inline-подтверждением → первый цикл не шлёт ложной
   дельты (база=цена добавления)».
7. **Handlers** — тонкие: extract_sku-роутинг, тексты ошибок, лимит-100, дубль-
   сообщение, `/list` = N сообщений по 1/сек через fake sender.

Решения, которые проверяются именно на швах, а не в Implementation, — это весь
смысл: ни один тест не знает про SQL-запросы и JSON WB (парсинг закрыт фикстурами).

---

## 6. Что сознательно НЕ проектируем

- **Очереди / Celery / Redis** — отвергнуто ADR-0002; один цикл, один процесс.
- **Кэш/проксирование фото WB** — `photo_url` отдаётся Telegram как ссылка; отдельный модуль кэша не заводится.
- **Web-админка, дашборды, статистика** — вне домена бота.
- **Мультипроцесс/масштабирование воркера** — второй процесс сломал бы SQLite-подход (ADR-0002).
- **Редирект-резолвер коротких ссылок `go.wb.ru`** — политика parsing явно отказывается от угадывания (см. §7 ADR-кандидат).
- **Отдельный retry-фреймворк**: единственные ретраи — FloodWait внутри NotifySender и отсутствие ретраев на WB (пропуск региона до следующего Цикла).
- **Удаление товаров из БД** — «нет карточки» выражается состоянием Отслеживания, строки `products`/`product_prices` не вычищаются (кроме price_history prune).

---

## 7. Возможные ADR (новые необратимые решения — сюда, сами ADR не создаём)

1. **Layout `src/app` и вынос `notify/` из `bot/`** — направление зависимостей
   worker→notify без импорта handlers; обратный путь = перекомпоновка пакетов.
2. **Справочник Регионов — статический словарь в config** (город→dest):
   публичного источника списка dest нет (research wb-regions §1), решение
   «хардкод + ручной список (по умолчанию Курск)» необратимо для UX команды
   `/region`, пока не появится подтверждённый токен-эндпоинт.
3. **Store как один Module (единый repository), а не набор репозиториев по таблицам** — отказ от per-entity Repository; обратный путь = разбиение интерфейса на 5.
4. **Отсутствующий в ответе nm трактуется как Недоступный товар, а не как удаление карточки** — влияет на `unavailable_since`, историю, «Товар не найден» только в флоу добавления.
5. **Флаг одиночной отправки живёт в `subscriptions.unavailable_notified`** и сбрасывается при возвращении цены (схема поверх дизайн-контракта — зафиксировать).
6. **Политика парсинга: несколько разных чисел = отказ, не «старшее»** — необратимое UX-решение (дубль/ложный артикул).
7. **Ошибка региона в Цикле проверки = пропуск региона без ретраев до следующего Цикла** — компромисс «не злить WB x-pow-челленджем» против «не пропустить падение цены».
8. **Вся денежная арифметика — int-копейки во всех швах** (Interface-инвариант Store/Client/decide); переход на рубли/Decimal в интерфейсах считается разрушительным.
