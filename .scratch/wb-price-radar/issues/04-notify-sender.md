# 04: NotifySender: доставка сообщений с троттлингом и результатами вместо исключений

**What to build:** Единственный способ отправить сообщение пользователю — шов `NotifySender` (`docs/architecture.md` §2.4): вызывающий (бот или воркер) передаёт `OutgoingMessage`, всегда получает результат `Delivered | UserBlocked | Failed` и никогда не ловит исключение. Глобальный троттлинг ≥ 1 msg/s между любыми send и FloodWait-ретраи с лимитом попыток (3) спрятаны внутри шва; `UserBlocked` (Forbidden, пользователь заблокировал бота) — сигнал вызывающему, деактивацию выполняет не шов. Один экземпляр на процесс — иначе разъедется троттлинг между bot и worker.

**Blocked by:** 01 (каркас проекта)

**Status:** accepted

- [x] Interface `NotifySender.send(OutgoingMessage) -> DeliveryResult` по §2.4; значения `Delivered | UserBlocked | Failed(exc_summary, attempt_count)` — frozen
- [x] Никогда не бросает наружу ошибки доставки
- [x] Глобальный троттлинг ≥ 1 msg/s между любыми вызовами send
- [x] FloodWait ретраится с уважением к retry_after, после лимита попыток (3) → `Failed`, без зависания
- [x] Forbidden → `UserBlocked` как сигнал (никакой деактивации внутри шва)
- [x] Экземпляр создаётся один в точке входа и инжектируется всем вызывающим
- [x] Тесты на fake-транспорте с инъектируемыми Forbidden/FloodWait и фейковыми часами (spec, «Решения по тестированию» п.4): 10 сообщений → ≥ 9 секунд фейкового времени; Forbidden → `UserBlocked` без исключения; FloodWait → ретраи → `Failed`

## Comments

- Реализация: `src/app/notify/interface.py` (Protocol + frozen-иерархия),
  `src/app/notify/telegram.py` (`TelegramNotifySender`: инъектируемые `bot`,
  `clock`, `sleep`, `throttle_interval=1.0`, `max_attempts=3`; `asyncio.Lock`
  сериализует любые send, троттлинг проверяется перед КАЖДЫМ sendMessage,
  включая ретраи).
- Тесты: `tests/notify/test_telegram_sender.py` — 8 тестов, TDD red-green
  (каждый падал до реализации); fake-бот с инжекцией исключений, fake-часы без
  реального сна (суммарное время исполнения тестов <5 мс; ~4.5 с сессии —
  холодный импорт aiogram).
- Критерий «экземпляр один в точке входа» не отмечен: композиция в `main.py` —
  вне объёма тикета (main.py не трогался по заданию); шов к этому готов
  (состояние троттлинга в экземпляре).
- Команда: `.venv\Scripts\python.exe -m pytest -q` → `55 passed`
  (включая `8 passed` в `tests/notify`).
- 2026-09-28, закрытие последнего критерия: композиция в `src/app/main.py`
  (`run_app`) собирает единственный `TelegramNotifySender(bot)` и передаёт его
  и `BotDeps` handler'ов, и `run_check_cycle_loop` — один экземпляр на процесс,
  троттлинг не разъедется. Покрытие: `tests/test_main_composition.py`
  (assert by one: identity `deps.sender is sender` через замыкания handler'ов +
  ровно один вызов конструктора Sender). Тикет закрыт.
