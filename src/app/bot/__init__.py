"""Тонкий UI-слой бота (docs/architecture.md §2.5).

Правило слоя: ввод -> extract_sku/domain -> Store/MarketplaceClient ->
форматирование ответа. Никаких SQL, httpx и бизнес-решений внутри handlers.
"""
