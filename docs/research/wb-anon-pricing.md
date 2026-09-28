# Research: обезличенные (без авторизации) цены WB и региональная цена

Дата проверки: 2026-09-28. Метод: прямые запросы к эндпоинтам WB (curl, анонимно, без cookies/токенов) + первичные исходники (github-парсеры, официальная документация). Код не писался — только разведка.

TL;DR: **Да, анонимную региональную цену получить можно** — через `card.wb.ru/cards/v4/detail?...&dest=<id региона>`. Финальная цена (`price.product`) реально различается по `dest` без всякой авторизации (замерено: Москва 61 676 ₽ vs СПб 60 640 ₽ на одном nm в одну минуту). Персональную «цену для вас» анонимно не видно. Старый эндпоинт `card.wb.ru/feedbacks/v1/{sku}` мёртв (404), живой аналог — `feedbacks1/2.wb.ru`, но он больше не отдаёт цены.

---

## 1. Карточка товара: feedbacks и cards

### 1.1 Что живое

| Эндпоинт | Статус (анонимно, 2026-09-28) | Цены? |
|---|---|---|
| `card.wb.ru/feedbacks/v1/{sku}` | **404** — хост/путь мертвы | — |
| `feedbacks1.wb.ru/feedbacks/v1/{sku}`, `feedbacks2.wb.ru/feedbacks/v2/{imt_id}` | 200, но пустая заготовка отзывов (277 байт даже на выдуманный SKU) | **нет цен** |
| `basket-NN.wbbasket.ru/vol.../part.../.../info/ru/card.json` (живой шард подобран: basket-28) | 200 | **нет цен** — в новой схеме (`nm_id`, `selling`, `properties`...) ценовых полей больше нет |
| `card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=...&spp=30&nm=...` | **200, цены есть** | основной источник цены |
| `search.wb.ru/exactmatch/ru/common/v9/search?...` | с нашего IP 403 (ботозащита/rate-limit; фикстуры 2026-08 показывают 200 с ценами) | цены в том же формате |
| `search-goods.wildberries.ru/search?query=...&dest=...` | 200 | только массив nmID, без цен |

Актуальность `cards/v4` подтверждена живыми фикстурами парсер-коннектора `Vladimir-Human/ru-marketplace-mcp` (захват 2026-08-07, см. provenance-файлы в репо).

### 1.2 Устройство цены в cards/v4 (и v9-поиске)

Цены лежат в `products[].sizes[].price`, значения в **копейках**:

```json
"price": {
  "basic": 8662900,     // базовая цена «до скидки» (перечёркнутая в UI)
  "product": 6167900,   // финальная цена товара для всех = цена с учётом региона/скидок
  "logistics": 0, "return": 0, "cashback": 0,
  "total": ...          // фолбэк-поле (в коде коннектора product OR total)
}
```

- `product` — «итоговая» цена, та, что покупатель реально видит в карточке без персональной скидки.
- `basic` — «исходная» цена до скидок (соотношение product/basic даёт суммарный discount).
- **Старые поля `priceU`/`salePriceU` (уровня товара) сейчас null** — это прямо закомментировано в `wb_connector/server.py:13-14` («WB v4: top-level priceU/salePriceU are NOW null. Real prices live in sizes[0].price.{product, basic} as integer kopecks»).
- Легаси-семантика (по README `RomanKovalev007/wb-parser`): `priceU` — цена без скидки, `salePriceU` — «цена со скидкой» (цена WB Клуба), обе в копейках.
- Поле `productDiscount` / `productPrice` в `productSummary` старого `card.wb.ru/feedbacks/v1` **проверить не удалось**: эндпоинт мёртв, в WaybackMachine снапшотов нет, в актуальном коде парсеров не встречается. Их функциональные аналоги в новой схеме — `price.product` (цена товара) и скидка, вычисляемая из `basic`/`product`. Сейчас в обезличенном ответе нет отдельного «WB Club salePrice» — финальная цена уже единая (`cashback=0` в замерах).
- Бонус-поля: `sizes[].wh` (склад), `dist` (киометры логистики до склада), `stocks[].qty` — меняются вместе с `dest`, т.е. регион влияет и на склад-источник, и на цену.
- Важный готч из фикстур: **цена в поиске и в карточке может различаться на одном и том же nm в одну минуту** (55 621 ₽ в search v9 vs 54 676 ₽ в cards v4, 2026-08-07; в репо это зафиксировано как «documented search-vs-card gap»). Для «итоговой цены» надо выбрать один источник (обычно cards).

## 2. Эндпоинт `/api/v2/list/goods/filter`

- Актуальный официальный хост: **`discounts-prices-api.wildberries.ru`** (это API «Цены и скидки» продавца; документация: `openapi.wb.ru/prices/api/ru`, по ссылкам из `Dakword/WBSeller/docs/Prices.md`). Хост `indices-data-315045858578.wildberries.ru` из вопроса **не резолвится** (DNS не отдаёт адрес; ни в одном актуальном парсере не встречается — grep.app/поиск пусто). `apidocs.wildberries.ru` тоже нерезолвящийся.
- **Авторизация: обязательна.** Анонимный запрос возвращает проверенное: `HTTP 401 {"statusText":"Unauthorized","detail":"invalid API access token: empty main token"}`. Нужен токен продавца со скоупом `prices`, и он отдаёт **только товары этого продавца** — для чужих nmID бесполезен.
- Пакетность: да — GET c `filterNmID` (один nm) или **POST `{"nmList":[...]}`** (батч), `limit`/`offset`; размеры — `/api/v2/list/goods/size/nm?nmID=...` (по `RTHeLL/wbsdk/src/wbsdk/api/prices.py:95-126`).
- `dest`/`cainfo`/`wb-region-id` на это API не влияют — цена там seller-side (установленная продавцом), без региональной персонализации.
- Для анонимного батча вместо него: `card.wb.ru/cards/v4/detail` принимает **повторяющийся параметр `nm=`** (проверено: `nm=A&nm=B` и `nm=A;B` → 200 на 2 товара; `nm=A,B` через запятую → **400**).

## 3. Влияние региона и персонализации (живые замеры 2026-09-28)

Один и тот же nm 1280469586, анонимно, одна минута:

| dest | регион | basic | product (финальная) |
|---|---|---|---|
| `-1257786` | Москва | 86 629 ₽ | **61 679 ₽** |
| `-1029256` | Санкт-Петербург | 86 629 ₽ | **60 640 ₽** |
| (без dest) | — | `{"products":[]}` — пустой ответ, цены нет | |

- `basic` не зависит от dest, `product` — **зависит** (региональная надбавка/скидка видна анонимно).
- Заголовки `cainfo` и `wb-region-id` анонимный ответ **не меняют** (product остался 61 679 ₽) — без пользовательских cookies персонализация не включается.
- Персональные скидки («цена для вас», по истории пользователя) живут во фронтендовых internal-эндпоинтах с cookies и `x-wbaas-token` (см. `MrJMark/Wb-Extension`: `www.wildberries.ru/__internal/u-search/exactmatch/ru/common/v18/search` с Cookie-заголовком). В обезличенном API видна только **общая региональная** цена — то есть ровно то, что видит новый анонимный пользователь из региона N.
- Обязательные параметры анонимных запросов: `appType=1`, `curr=rub`, `locale=ru`, `dest`, часто `spp=30`; без них — пустые цены/стокс или ботоприкол (Cloudflare/«Angie»-баннер). `dest=-1257786` (Москва) — канонический дефолт у всех парсеров (`server.py:17`: «WITHOUT dest: empty stocks/wrong prices/Cloudflare HTML»). Список city→destID хранят сами парсеры (Москва `-1257786`, СПб `-1029256`; полный гео-справочник WB не документирован публично).

## 4. Итог

- **Региональную финальную цену — да, анонимно**: `GET https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=<destID региона>&locale=ru&spp=30&nm=<nmID>` (батч через повтор `nm=`). Цена в `sizes[].price.product` (копейки), «до скидки» в `price.basic`. Тот же формат цены у `search.wb.ru/exactmatch/ru/common/v9/search` (нужны аккуратные заголовки/ротация, склонен к 403/429).
- **Персональную цену — нет**: требует авторизованной сессии пользователя; `dest` даёт только «общедоступную региональную» цену.
- `/api/v2/list/goods/filter` (discounts-prices-api.wildberries.ru) — не для этой задачи: 401 без токена продавца, и только свои товары.
- Юридическая ремарка: использование неофициальных фронтовых API противоречит п. 9.9.6 оферты WB (официальный ответ на форуме `dev.wildberries.ru/forum/topics/1313/card-wb-ru`) — для промпарсинга оценить риски.

## Источники

- Живые запросы к `card.wb.ru/cards/v4/detail`, `feedbacks1.wb.ru`, `basket-28.wbbasket.ru`, `search-goods.wildberries.ru`, `discounts-prices-api.wildberries.ru` (2026-09-28, curl, анонимно).
- `Vladimir-Human/ru-marketplace-mcp` — фикстуры card_v4/search_v9 с provenance (захват 2026-08-07): `packages/wb-connector/tests/fixtures/*.json`; `src/wb_connector/server.py` (семантика dest, цена в копейках, устаревание priceU/salePriceU). https://github.com/Vladimir-Human/ru-marketplace-mcp
- `glmn/wb-private-api` — каталог фронтовых URL (легаси-хосты card/feedbacks/basket). https://github.com/glmn/wb-private-api/blob/main/src/Constants.js
- `Dakword/WBSeller/docs/Prices.md` — соответствие `/api/v2/list/goods/filter` = официальное «Цены и скидки» (openapi.wb.ru/prices/api/ru). https://github.com/Dakword/WBSeller
- `RTHeLL/wbsdk/src/wbsdk/api/prices.py` — батч `nmList`, `filterNmID`. https://github.com/RTHeLL/wbsdk
- `RomanKovalev007/wb-parser` — легаси-семантика `priceU`/`salePriceU` (копейки, «цена со скидкой»). https://github.com/RomanKovalev007/wb-parser
- `MrJMark/Wb-Extension` — internal u-search v18 с cookies/x-wbaas-token (персонализация). https://github.com/MrJMark/Wb-Extension
- `dev.wildberries.ru/forum/topics/1313/card-wb-ru` — позиция WB про п. 9.9.6 оферты (через сниппет поиска; сам домен частично недоступен из текущей сети).

## Не проверено / открытые вопросы

- Точная структура легаси-`productSummary` (`productDiscount`/`productPrice` в feedbacks/v1) — эндпоинт мёртв, снапшотов нет; в актуальных ответах аналогов этим полям два (basic/product) — см. п.1.2.
- Полный список `dest` для всех регионов — справочник гео WB не найден публично; известен для Москвы/СПб + практика парсеров.
- Влияние `spp=30` на цену не изолировано (все рабочие примеры его передают).
