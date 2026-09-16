# Release candidate — OhMySmell

Дата: 2026-09-16. Ветка: feature/sales-core-v2. Application checkpoint: 8faf4d5.
Production и main не изменялись. EXTERNAL_WRITES_ENABLED=false в API и worker.

## Что готово

- Manager Telegram workspace: русское меню, review/ready drafts, новые/в сборке/
  собранные/отгруженные/отменённые Orders, paid/unpaid, поиск и страницы списков.
- Карточка: имя, email, телефон/Telegram, source, customer type, контрагент,
  товары/qty/цены/итог, складские планы, сборка/оплата/доставка. Большие заказы:
  `/items ID`. После finalize — «Открыть заказ».
- Fulfillment new → assembling → assembled → shipped; independent unpaid → paid.
  Actor/timestamps, paid_at/paid_by_manager_id, payment_note, audit events.
  Проблема/проверка блокирует сборку. Отмена до оплаты/отгрузки — с подтверждением.
- Delivery method/reference/status хранятся локально. Выбор СДЭК/Яндекс не вызывает API.
- Client channel: /start → /request → товары/qty → контакт → /send → номер;
  /status только для своей заявки, /manager для связи. Persistent conversation,
  update deduplication, existing identity priority, неизвестный тип — manager review.
- Role separation: отдельные dispatchers/tokens. Клиентские callback никогда не
  достигают manager handlers. Цена и privileged status не принимаются от клиента.
- Idempotency: finalize, direct internal Order, operational actions и client updates.
  Row locks, revision checks, durable audit, concurrency tests. Все суммы — копейки.
- Два склада: полный deterministic split, недостаток отклоняется атомарно,
  складские блоки показываются менеджеру; внешних резервов нет.
- Runtime: отдельный Railway manager service, одна replica и DB session lock на bot ID,
  health/structured startup, controlled shutdown, отдельная очередь уведомлений.
  FastAPI не запускает Telegram polling. API catalog/checkout и OpenAPI сохранены.

## Проверки

Итоговые результаты и измерения: [Remote acceptance](REMOTE_STAGING_ACCEPTANCE.md).
**237 tests passed**: 226 unit и 11 PostgreSQL; дополнительно 7 subtests.
Unit suite, PostgreSQL suite, compileall, fresh migration rehearsal, existing staging
upgrade, read-only Alembic check и diff/secret checks пройдены.

Миграция: j93d5087bc10 после i82c4f76ab09. Additive order fields/indexes, audit,
client conversations/updates, source=telegram. Без stamp и destructive downgrade.
Email карточки — read-only projection, новая identity для него не создаётся.

## Остались подключения и activation

1. Gmail: отдельные OAuth client/token, consent владельца, read-only ingestion smoke,
   включение отдельного email worker и проверка duplicate/cursor recovery.
2. Tilda: финальная страница и exact HTTPS origin → CORS → browser checkout smoke.
   В браузере только public catalog/checkout и persistent Idempotency-Key.
3. Credentials/config: production DB/internal token/manager token и manager allowlist;
   отдельный client Telegram token для включения дополнительного live канала;
   retail price mapping, если нужен priced retail Order вместо заявки с review.
4. Controlled production activation: отдельное разрешение, backup/restore и migration
   rehearsal на копии, restricted runtime role, final origin, ingress rate limits,
   monitoring/alerts, one worker replica, BOT_PRODUCTION_ACTIVATED=true только тогда.
   Canary checkout/manager workflow с external writes=false, наблюдение и rollback.

MoySklad writes, настоящие CDEK/Yandex orders и платежи не включены. Их будущая
активация требует отдельной приёмки и разрешения. Payment tracking здесь — только
ручная отметка факта оплаты менеджером, без банка и эквайринга.

## Known limitations

- Нет отдельного client token: клиентские updates проверены fake transport с реальной
  staging DB, запуск client service не выполнялся. Код/runtime готовы.
- Розничного прайса нет: клиент получает заявку с проверкой, оптовая цена не раскрывается.
- Первый live склад пуст: live two-store split не воспроизводится; fakes/PostgreSQL
  подтверждают split и отказ частичного плана. Реальные остатки не менялись.
- Telegram callbacks в E2E синтетические, send/edit настоящие. Человеческое нажатие
  физической Telegram-кнопки не выдается за проведённый тест.
- Notifications at-least-once: после аварии возможен повтор сообщения, но не повтор
  изменения заказа. Local warehouse plan не является внешним резервом.
- Production инфраструктура и поведение не проверялись и не активировались.

## Exact checklist

- [x] Manager daily workspace и русские ошибки/контекстные кнопки.
- [x] Fulfillment/payment/delivery fields, timestamps, actor, audit.
- [x] Client request/status/contact layer и role separation.
- [x] Customer normalization и приоритет существующего профиля.
- [x] Race/idempotency/stale callbacks/concurrent finalize.
- [x] Warehouse one/two/insufficient/full allocation tests.
- [x] Isolated manager staging worker, health, singleton ownership.
- [x] API/OpenAPI/security/secret checks.
- [x] Fresh и existing staging migration checks.
- [x] Unit/PostgreSQL tests и staging A/B/C E2E.
- [x] Real Telegram send/edit и performance measurements.
- [x] Documentation, feature branch push, staging deploy.
- [ ] Gmail OAuth и email worker activation.
- [ ] Final Tilda origin/CORS/browser acceptance.
- [ ] Недостающие production/channel credentials и бизнес-конфигурация.
- [ ] Отдельно разрешённая controlled production activation.

Инструкции: [Manager](MANAGER_BOT.md), [Client](CLIENT_BOT.md),
[Lifecycle](ORDER_LIFECYCLE.md), [Operations](OPERATIONS.md), [Tilda](TILDA.md).
