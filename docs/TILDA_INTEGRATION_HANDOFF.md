# Передача интеграции Tilda разработчику

Реальная Tilda пока не подключена. `TILDA_ENABLED=false`,
`ORDER_DESK_SEND_ENABLED=false`, `EXTERNAL_WRITES_ENABLED=false`.
Изменение Railway, публикация страницы и отправка сообщений требуют отдельного разрешения.

## Подтверждённые возможности и границы контракта

[Документация Tilda Webhook](https://www.help.tilda.cc/forms/webhook) подтверждает
HTTPS POST, настройку передачи товаров массивами и отдельную опцию externalid при
включённых массивах. Адрес подключается в Site Settings → Forms → Webhook, затем
выбирается у формы. Ответ должен поступить в течение пяти секунд; описаны две повторные
попытки с минутным интервалом. Backend сохраняет заявку и outbox до ответа, Telegram
не вызывается внутри webhook.

Документация не является схемой payload конкретного магазина. Имена `tranid`,
`products`, `amount`, `payment.products`, `sku` в fixtures — **синтетические контракты
адаптера**, а не утверждение, что каждый native webhook содержит эти поля.
Необходимо получить обезличенный реальный capture тестовой страницы, включая retry.
SKU и externalid не взаимозаменяются автоматически. Для SKU нужна явная item map.
Произвольный текст корзины не разбирается эвристиками.

Проверяемые варианты в `tests/fixtures/tilda/contracts.json`:

| Формат | Пример структуры | Настройка |
|---|---|---|
| JSON адаптера | `products: [...]` | flat defaults |
| Form | `products` как JSON-строка | flat defaults |
| Form, массив | `products[0][externalid]` | flat defaults |
| Form, вложенный массив | `payment[products][0][externalid]` | items/total paths |
| JSON, envelope и SKU | `payment.products[].sku` | items/total paths + `id: sku` |
| Form, envelope строкой | `payment` как JSON | items/total paths |

Парсер отвергает дубли ключей, смешение scalar/object, пропуски и неоднозначные индексы.
Неизвестные обычные поля игнорируются при нормализации; они не участвуют в дедупликации.

## Endpoint и безопасность

`POST https://ohmysmell-backend-staging-staging.up.railway.app/integrations/tilda/orders`

Предпочтительно: доверенный серверный adapter передаёт `X-Tilda-Secret`.
`TILDA_WEBHOOK_SECRET` — отдельный случайный секрет, минимум 32 символа; не bot token.
Нельзя класть его, `INTERNAL_API_TOKEN` или credentials в JavaScript страницы.
Native webhook не считается поддерживающим произвольные headers без подтверждения.
Query-secret режим возможен только после отдельной проверки редактирования URL во всех
Railway/proxy/edge/access логах. Редактирования только внутри приложения недостаточно.

Коды: 202 — новая заявка; 200 — повторная доставка; 401 — неверный секрет;
409 — тот же external ID с другим нормализованным содержимым; 415 — тип тела;
422 — невалидная корзина/структура; 503 — intake выключен или конфигурация некорректна.
В ответе нет Telegram link, контактов или секрета. Не логировать полный payload.

## Mapping и тестовый заказ

Пример адаптера, не подтверждённый capture сайта:

```json
{
  "tranid": "synthetic-unique-checkout-001",
  "Name": "SYNTHETIC Buyer",
  "Email": "synthetic@example.invalid",
  "amount": "30.30",
  "currency": "RUB",
  "products": [
    {"externalid": "sku-a", "name": "SYNTHETIC A", "quantity": "2", "price": "10.10"},
    {"externalid": "sku-b", "name": "SYNTHETIC B", "quantity": "1", "price": "10.10"}
  ]
}
```

Настройки должны совпадать на backend и manager при `/sitereprice`:

```text
TILDA_FIELD_MAP_JSON={"external_id":"tranid","items":"products","total":"amount"}
TILDA_ITEM_FIELD_MAP_JSON={"id":"externalid","name":"name","qty":"quantity","price":"price"}
TILDA_PRODUCT_MAP_JSON={"sku-a":{"product_id":"verified-catalog-id-a","retail_price_minor":1010},"sku-b":{"product_id":"verified-catalog-id-b","retail_price_minor":1010}}
```

Для envelope: `{"items":"payment.products","total":"payment.amount"}`.
Для SKU: `{"id":"sku"}`. Ни один вариант не включать без сверки с capture.
В product map нужны реальные **существующие** catalog IDs и проверенная розничная цена
в minor units. Примерные IDs выше нельзя использовать для реального checkout.

Любой заказ Tilda — RETAIL, даже у wholesale-профиля. Wholesale fallback отсутствует.
Цены parsing — Decimal; сохранение — integer minor units. Допустимы целые положительные
количества. Неверные/отсутствующие цены, суммы, скидки, другая валюта и отсутствующий mapping
требуют review; заявленная сумма не становится доверенной автоматически. Согласование
менеджера принимает проверенную розничную сумму позиций, а не цену из пользовательского тела.
Непроверенные match/quantity, пустая корзина и неположительная цена блокируют подтверждение.
Согласование, Order, аудит и уведомление сохраняются одной транзакцией.

Внешний ID должен сохраняться при retry, но отличаться у независимых checkout с одинаковой
корзиной. Без стабильного ID требуется серверный `Idempotency-Key` и его хранение адаптером.
Хеш корзины нельзя использовать как идентичность заказа.

## Success page и Telegram

Нужен доверенный bridge, который сначала проверяет владение checkout по серверной сессии
или собственному одноразовому credential, затем вызывает защищённый
`POST /internal/tilda/drafts/{draft_id}/telegram-link`.
Нельзя выдавать ссылку по одному последовательному draft ID. Native webhook response
не считается доступным браузеру, динамический redirect Tilda не предполагается.

Backend выдаёт `https://t.me/<username>?start=<opaque_token>`: 32 случайных байта,
43 base64url-символа, в БД только SHA-256, TTL настраивается. Это укладывается в
[ограничение Telegram](https://core.telegram.org/bots/features#deep-linking) в 64 символа.
Bridge отдаёт link только владельцу checkout, с no-store и no-referrer, без аналитики URL.
Покупатель сам открывает бота и нажимает Start. Повторный Start того же пользователя
показывает текущий статус; другой пользователь не получает чужую заявку.
Заказ сохраняется независимо от перехода в Telegram. Реализация доверенного bridge
на стороне сайта остаётся отдельной интеграционной работой.

## Acceptance и rollback

1. Проверить capture и fixtures, retail map, identity при retry и независимых корзинах.
2. Получить отдельное разрешение на тестовую страницу, временный intake и отправку.
3. Проверить адресатов и очереди. Для staging указать разрешённых recipient IDs,
   `ORDER_DESK_STAGING_DRAFT_IDS` и новую временную границу
   `ORDER_DESK_STAGING_NOT_BEFORE=<UTC ISO-8601 с timezone>`.
   Старые записи разрешаются только отдельно проверенными `ORDER_DESK_STAGING_MESSAGE_IDS`.
   **Сообщение №7 не включать**. Флаг sending сам по себе не даёт права доставки.
4. Проверить webhook/retry, одну карточку, Start/link, claim, сообщения в обе стороны,
   подтверждение, оплату, сборку, доставку и совпадение статусов на всех карточках.
5. Отдельно проверить ошибки, неизвестный товар, расхождение суммы и недоступность Telegram.
6. Выключить intake/sending, восстановить временные Variables, дождаться готовности workers;
   сверить очередь и audit. Не удалять uncertain/pending записи ради пустой очереди.
7. При откате кода оставить оба флага выключенными: предыдущая версия не учитывает новую
   временную границу scope. Историю/Order не откатывать. Схема БД этой доработкой не меняется.

МойСклад, автоматические резервы, списания, реальные платежи и синхронизация
Tilda → MoySklad остаются отключёнными. Все физические операции выполняются вручную.
