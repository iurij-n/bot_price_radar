# 06: Бот: /list и удаление Отслеживаний кнопкой 🗑

**What to build:** Пользователь отправляет `/list` и получает по одному сообщению на каждый отслеживаемый Товар: фото, название, текущая Финальная цена в его Регионе, кнопка 🗑. Выдача идёт через `NotifySender` (троттлинг ≥ 1 msg/s уже внутри шва), FloodWait/сбой доставки одного сообщения не роняет всю выдачу. Нажатие 🗑 удаляет Отслеживание: Уведомления по этому Товару больше не приходят, а история цен пары Товар×Регион сохраняется, пока на пару есть другие Отслеживания (prune — ответственность Store, spec, пользовательские истории 16–18).

**Blocked by:** 05 (бот: добавление Отслеживания)

**Status:** accepted

- [x] `/list` отдаёт N сообщений на N Отслеживаний, каждое — фото, название, Финальная цена в Регионе пользователя, кнопка 🗑 *(фото сознательно убрано: seam NotifySender умеет только текст+кнопки — см. Comments; строка = название + Артикул + цена + «🔗 Открыть товар» + «🗑 Удалить»)*
- [x] Все send идут через `NotifySender`, отдельного rate-limit в handler нет
- [x] `UserBlocked`/`Failed` при выдаче /list логируются, но не прерывают выдачу остальных сообщений; `UserBlocked` → `deactivate_user` (см. также тикет 10) *(Failed — лог и продолжение; UserBlocked — деактивация и остановка: заблокированный пользователь всё равно не получит остальное)*
- [x] 🗑 → `Store.remove_tracking`; повторный /list без товара; удаление последнего Отслеживания пары запускает prune истории, наличие других Отслеживаний пары — историю сохраняет *(prune — ответственность Store, покрыт tests/store: test_remove_tracking_prunes_history_only_at_zero, test_remove_tracking_keeps_history_for_inactive_subscription, test_remove_tracking_prunes_only_own_dest — здесь не дублируется)*
- [x] После удаления Уведомления по Товару этому пользователю не порождаются *(подписка удалена → notification_candidates/unavailable_due её не видят; идемпотентность: неизвестная пара — тот же ответ «Удалено»)*
- [x] Handler-тесты на fake-швах: N товаров → ровно N send; после удаления `find_tracking` → None (spec, «Решения по тестированию» п.7) *(tests/bot/test_list_remove.py; «find_tracking → None» проверен через list_trackings/removed на fake-сторе, реальный find — в тестах Store)*
- [ ] Ручной демо-прогон (пометить manual): /list на 25+ товарах — троттлинг виден, ничего не падает

## Comments

- **Композиционный разрыв (PART A):** SqlStore не имел `get_user_dest`, который
  bot-слой уже вызывал (handlers/confirm) через локальный подпротокол `BotStore`.
  Метод добавлен в `Store` Protocol и `SqlStore` (одна read-транзакция,
  SELECT User.dest); `BotStore` из handlers.py удалён — один интерфейс;
  `type: ignore` в main.py снят; тест `test_get_user_dest_returns_current_dest_or_none`.
- **Решение по кнопке 🗑:** `OutgoingMessage` расширен необязательным полем
  `callback: (label, callback_data)` — второй строкой рисует `TelegramNotifySender`
  (тесты в tests/notify). Поле с дефолтом None — существующие вызовы (worker,
  deliver) не затронуты. Фото в /list через шов не проходит (§2.4) — решение:
  текстовые строки, URL-кнопка остаётся.
- **Формат строки:** `messages.tracking_text(row)` — «название\nАртикул: nm\n
  Цена: X ₽»; `current_price_kop is None` (нет строки product_prices ИЛИ
  Недоступный товар) — один текст «Сейчас нет цены в вашем регионе»: TrackingRow
  эти случаи не различает, отдельного сигнала в интерфейсе нет.
- **callback_data:** `del:<product_id>`, кодек `encode_del/decode_del` в
  keyboards.py, проверка ≤64 байт, мусор → ответ на callback без side-effects.
- **Обработка результата удаления:** `edit_text(«🗑 Удалено…») + edit_reply_markup(None)`
  best-effort (suppress TelegramAPIError), `callback.answer()` первым делом —
  как в confirm.py.
- Файлы: src/app/store/repository.py, src/app/bot/handlers.py, src/app/bot/keyboards.py,
  src/app/bot/messages.py, src/app/notify/interface.py, src/app/notify/telegram.py,
  src/app/main.py, docs/architecture.md (§2.2/§2.4/§2.5),
  tests/store/test_repository.py, tests/notify/test_telegram_sender.py,
  tests/bot/test_list_remove.py (новый). Прогон: 170 passed.
