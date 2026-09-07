# OhMySmell — FINISH REPORT

Дата: 2026-09-07. Ветка: feature/sales-core-v2.

Подготовлен и проверен законченный staging sales core с versioned website intake,
manager review, локальными заказами и fulfillment plans. Основная staging DB
обновлена. Production DB/deployment, Tilda, реальный Gmail OAuth и внешние
бизнес-записи не затрагивались. Это готовность в разрешённом staging scope;
production activation остаётся контролируемым отдельным этапом.

## 1. Что готово

- Audit кода, архитектуры, DB, API, concurrency, security, integrations и performance.
- Закрыт публичный legacy /orders, запрещены frontend price/type authority и
  изменение существующего профиля через checkout.
- Customer/identities, email/channel intake, matching, integer pricing, draft review,
  local finalize/reject, persisted website idempotency и warehouse allocation.
- Telegram списки, карточки, поиск товара /match, pagination /drafts, versioned
  callbacks, early ACK, async auth/HTTP, no-op и stale callback handling.
- Guarded customerorder/demand payloads, persistent external-operation journal,
  delivery models/service/adapters, notification retry queue.
- Tilda contract, OpenAPI snapshot, environment/deployment/migration/rollback runbooks.
- Устранены 1474 отслеживаемых файла venv из Git index, сам venv сохранён. Исправлен
  UTF-16 requirements.txt; обновлены уязвимые зависимости.

## 2. Итоговая архитектура

Channels → Customer/Identity → matching → PriceService → InboundMessage/Draft →
manager review → local Order → stock snapshot → Shipment/WarehouseAllocation.
Website использует тот же draft/review/finalize core и отдельную atomic checkout
idempotency boundary. ExternalOperation отделяет локальный intent от будущего
guarded HTTP. Подробнее и схема: [ARCHITECTURE.md](ARCHITECTURE.md).

## 3. Реализованные flows

| Flow | Результат |
|---|---|
| FakeEmail / Gmail readonly architecture | Parsed lines, customer, matching, pricing, draft |
| Unknown customer / ambiguous product / missing price | Controlled review |
| Exact article/code/name, localized suffix | Однозначный match; fuzzy только кандидаты |
| Telegram/internal manager review | Тип клиента, товар, контрагент, reject/finalize |
| Concurrent finalize / duplicate email | Один Order / один draft |
| Website catalog/checkout | Server stock/price validation, 202 review, durable replay |
| Retail без price type | total_minor=null, без wholesale fallback |
| Два склада | Persisted split plans, one Shipment per order/store |
| MoySklad export | Guard + pure payload + durable intent; fake-tested |
| CDEK/Yandex/manual | Local delivery draft, guarded adapters; fake-tested |
| Instagram/Telegram client | Adapter contracts + trusted ingest_channel, без live transport |
| Notification recovery | Durable queue, lease, retry/backoff, at-least-once delivery |

## 4. Tests и validation

- **168 unit/contract tests passed**, 7 subtests passed; 6 PostgreSQL tests штатно
  skipped в обычном запуске, чтобы исключить случайный доступ к реальной DB.
- **6/6 opt-in PostgreSQL integration tests passed** в изолированной staging schema;
  последний запуск 96,69 секунды. Совокупно 174 теста + 7 subtests.
- Полный pytest, compileall app/tests/alembic/scripts, git diff --check — прошли.
- Fresh Alembic upgrade head и строгий alembic check — прошли на PostgreSQL с нуля.
- Upgrade/check основной staging DB — прошли, head i82c4f76ab09.
- pip check — no broken requirements. OSV: 56 pinned dependencies, 0 findings
  после aiohttp 3.14.3, cryptography 50.0.0, pyasn1 0.6.4.
- Скан reviewable Git tree на существующие local credentials — 0 findings.

Тестовая защита запрещает реальную HTTP-сеть и DB connections в unit tests.
Изначально один устаревший mock попытался открыть DB; соединение было заблокировано,
но pytest показал credential в traceback. Исправлены ранняя подстановка фиктивного
DB URL и безопасный traceback. **Этот DB credential необходимо сменить до production
activation.** Он не записан в код, tests, docs или Git; ротация не выполнялась.

## 5. Performance

| Измерение | Результат |
|---|---|
| set_customer_type до дополнительной оптимизации, без пула | 10 066,88 ms; 7 SELECT, 9 SQL statements |
| Финальный set_customer_type, с пулом | 3 505,70 ms; 4 SELECT, 7 SQL statements |
| Live read catalog, 401 товар | 2 629,96 ms |
| Live email ingestion | 14 559,04 ms |
| Live finalize + повторный finalize вместе | 12 736,86 ms |

Замеры из локального Windows до Railway, не SLA production. Первые две цифры
отражают и SQL optimization, и включение пула, поэтому это не чистое сравнение
одного изменения. Live E2E снят до последнего удаления лишней загрузки при finalize.
Реальный Telegram client ACK RTT не измерялся; порядок early ACK/async auth и
отсутствие blocking I/O покрыты тестами. Полные action durations всё ещё зависят
от удалённой DB и МойСклад. Размещать runtime рядом с DB; мониторить p50/p95.

## 6. Staging E2E

Live read-only МойСклад: **401 active catalog product, 2 склада, 1 организация,
1 price type**. FakeEmail processed=1, failed=0 → **draft №4 → Order №2**,
**720 000 minor units = 7 200 RUB**. Повторный finalize вернул тот же Order.
MoySklad order ID NULL. Создан один local shipment plan; реальный split на два
склада проверен отдельным PostgreSQL тестом с детерминированным stock fixture.
**Одно Telegram-уведомление доставлено**. Реальных MoySklad documents не создано.

Website ASGI E2E с реальной staging DB/catalog: GET 200, checkout 202,
retail total null, identical replay, changed payload 409, internal без token 401,
с token 200. Создана отдельная помеченная STAGING WEBSITE TEST заявка.

Итог основной staging DB: 3 customers, 4 drafts, 2 orders. Исходный Order №1
сохранён: **1 328 000 minor units**, MoySklad ID NULL. Невалидных order item money
строк — 0. Собственные audit schemas и помеченные test records сохранены.

## 7. ENV variables

Полный справочник и defaults: [OPERATIONS.md — ENV](OPERATIONS.md#env-reference),
образец: [.env.example](../.env.example). Секреты не приводятся в отчёте.
Staging API token создан локально в игнорируемом файле; основной .env не переписан.

## 8. Database migrations

Три additive revisions поверх f59b1c43de76:
g60a2d54ef87 → h71b3e65fa98 → **i82c4f76ab09**.
Контакты website не теряются при finalize, schema/defaults соответствуют metadata.
Money CHECK constraints добавлены NOT VALID, защищают новые записи; historical
validation остаётся отдельным migration gate. Destructive downgrade запрещён.

## 9–10. API contract и инструкция Tilda-разработчику

Передать [TILDA.md](TILDA.md) и [openapi.json](openapi.json).
Маршруты: GET /api/v1/catalog, POST /api/v1/checkout. Контракт содержит payload,
response/error schemas, CORS, integer money, idempotency/retry и пример JavaScript.
Сайт получает только public data; internal token и MoySklad credentials ему не нужны.

## 11. Какие credentials ещё нужны

- Доступ владельца Gmail, OAuth client JSON и token с gmail.readonly.
- CDEK client ID/secret, Yandex Delivery token, согласованные tariff/address/packaging.
- Отдельные production internal/Telegram secrets, проверенные DB credentials.
- Credentials/API доступ Instagram, если решено подключать этот transport.
- Доступ к отдельному Railway staging API deployment, если нужен удалённый staging
  URL: текущий API E2E выполнен локально через ASGI с реальной staging DB.

## 12. После получения Tilda

Сопоставить product IDs, настроить реальные CORS origins, подключить catalog и
checkout, проверить мобильный UX/errors/retry, «Цена по запросу» и отсутствие
автоматической оплаты неопределённой цены. Изменений сайта пока не выполнялось.

## 13. Перед production

Ротация раскрытого DB credential; historical migration rehearsal; реальные origins,
runtime secrets, backup/alerts/WAF; согласованная retail pricing policy и warehouse
priority. Запуск worker/poller с отдельными токенами. Provider acceptance и
разрешение внешних writes выполняются отдельно. Docker build/deploy не проверялись.

## 14–16. Migration checklist, launch checklist и rollback

Полные пошаговые процедуры находятся в [OPERATIONS.md](OPERATIONS.md).
Не делать blind stamp, не применять рубли→копейки повторно, не делать автоматический
money downgrade, не повторять uncertain external POST без reconciliation.
Rollback сохраняет новые заявки и внешние operation intents; восстановление DB
не отменяет документы во внешних системах.

## 17. Known limitations

- Retail price пока отсутствует: менеджерская проверка вместо полноценной покупки
  с оплатой. Новая pricing policy не придумана.
- Allocation — локальный план без stock reservation; перед реальным export нужна
  свежая stock check и согласованный reservation flow, возможна конкуренция с
  другими каналами продаж МойСклад.
- Delivery adapters требуют реальных credentials, полных provider payloads и
  acceptance tests; quotes/tracking/cancellation не заявлены как готовые flows.
- Provider POST uncertain/inflight требует оператора; exactly-once external delivery
  не обещается. Telegram notifications могут повториться после crash.
- Постоянно некорректное email требует разбора; cursor сохраняется, поэтому
  проблемный batch повторяется. Dedicated dead-letter manager UI пока отсутствует.
- Bot имеет bounded lists и поиск товаров; это рабочий manager interface, не
  отдельная web CRM. Полный клиентский login, оплата и frontend находятся вне scope.
- Product cache process-local, не распределённый; large-scale нагрузочные тесты
  и production latency не измерялись. Rate limiting/anti-bot должны быть на edge.
- Новый код не был deployed в production или удалённый Railway API service.
  Staging DB и локальный API against staging проверены; bot poller не перезапускался.

## 18. Recommended next actions

1. Сменить раскрытый DB credential и безопасно обновить его у владельца.
2. Передать Tilda-разработчику контракт; подтвердить retail price и warehouse priority.
3. Поднять отдельный staging API runtime, notification worker и отдельный bot token
   для приёмки готового сайта; не использовать production deployment для теста.
4. Провести historical production migration rehearsal и launch checklist.
5. После отдельного разрешения выполнить provider canary и включать внешние flows
   последовательно, с journal reconciliation и проверкой stock.
