# 03: Store: персистентность Отслеживаний, цен и истории

**What to build:** Единый шов персистентности `Store`, говорящий на языке домена: пользователи с Регионом, Товары (уникальная пара Маркетплейс×Артикул), Финальные цены пар Товар×Регион с состоянием Недоступности, Отслеживания с Ценой сравнения и статусом, история цен. Схема и контракт методов — `docs/architecture.md` §2.2 и ADR-0003 (пересказ не нужен): add_tracking / remove_tracking / active_tracking_count / list_trackings / find_tracking / upsert_card / ensure_user / set_region / apply_batch / notification_candidates / unavailable_due / commit_notification / deactivate_user / reactivate_user / prune. Группировать реализацию можно, но интерфейс остаётся доменным, транзакции наружу не отдаются.

**Blocked by:** 01 (каркас проекта)

**Status:** accepted

- [x] Пять таблиц по ADR-0003 / `docs/architecture.md` §2.2 с уникальными парами: products (marketplace+Артикул), product_prices (Товар×dest), subscriptions (пользователь×Товар)
- [x] Interface `Store` по §2.2; каждый публичный метод — одна атомарная транзакция, сессии/commit наружу не видны
- [x] `apply_batch` идемпотентен по `now`: повтор без изменения цен даёт пустой diff, дублей history не создаёт
- [x] `set_region` применяет политику reset_comparison (Цена сравнения = Финальная цена в новом dest или None) и не трогает `product_prices`
- [x] `remove_tracking` делает prune `price_history` только когда на пару Товар×dest не осталось Отслеживаний, кроме удалённых; строки Товаров и цен не удаляются никогда
- [x] `deactivate_user` помечает Отслеживания неактивными без удаления строк; `reactivate_user` восстанавливает
- [x] Интеграционные тесты на `sqlite+aiosqlite:///:memory:` по всем инвариантам выше (spec, «Решения по тестированию» п.3): уникальные пары не дублируются; prune только при нуле Отслеживаний; set_region не трогает цены; apply_batch идемпотентен; deactivate_user не удаляет строки
- [ ] Fake in-memory реализация того же Interface для тестов bot/worker (spec, п.3) — отложена: по заданиюticket 03 не включает fake (будет в тикете bot/worker)

## Comments

Реализация: `src/app/store/{db,models,repository}.py`, тесты `tests/store/test_repository.py`
(37 тестов; вся сюита 92 passed). TDD red-green: Cycle A → Cycle B, каждый цикл начинался
с падающих тестов (21 failed → 37 passed).

Команды проверки:

```powershell
.venv\Scripts\python.exe -m pytest tests\store -q   # 37 passed
.venv\Scripts\python.exe -m pytest -q               # 92 passed
```

Принятые решения (зафиксированы в коде/тестах):

1. **remove_tracking = HARD-delete строки subscriptions** + prune `price_history` пары
   (product_id × dest удаляемого пользователя) только если на пару не осталось НИКАКИХ
   подписок (любой статус, включая inactive, любой пользователь). Строки products /
   product_prices не удаляются никогда (§6 архитектуры).
2. **commit_notification(subscription_id, new_comparison_kop: int | None,
   mark_unavailable_notified: bool = False)** — уточнённая сигнатура вместо размышлений
   про None: `new_comparison_kop is not None` → сдвиг Цены сравнения И сброс
   `unavailable_notified=False` (цена вернулась); `None` → Цену сравнения НЕ трогает
   (уведомление о Недоступном товаре базу не сдвигает); флаг ставится только явным
   `mark_unavailable_notified=True` (путь недоступного уведомления воркером, тикет 09).
3. **Недоступный товар не затирает `final_price_kop`**: availability/unavailable_since —
   состояние, последняя известная цена сохраняется; return цены с той же величиной даёт
   transition became_available БЕЗ дубля в price_history.
4. **Даты — UTC-aware через `UtcDateTime` TypeDecorator (impl=Text)**: SQLite нативно
   теряет tzinfo; канонический строковый формат леxicographically монотонен, SQL-сравнение
   `unavailable_since < cutoff` корректен.
5. **apply_batch привязан к маркетплейсу Adapter'а** (`SqlStore(sf, marketplace="wb")`):
   nm разных маркетплейсов не смешиваются (ADR-0004); неизвестный nm (карточки нет в БД)
   молча пропускается.
6. **add_tracking**: дубль → `DuplicateTrackingError` (caller сверяется через
   find_tracking); уникальный индекс в схеме подтверждён тестом прямой вставки
   (`IntegrityError`).
7. Пороги `unavailable_due`: строго «older than window» (`unavailable_since < now − window`);
   точная граница 48:00 проверяется в domain (`should_notify_unavailable`, тикет 09).
