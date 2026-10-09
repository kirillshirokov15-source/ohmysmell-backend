# Tilda → OhMySmell → Telegram: staging MVP

Актуальный staging go-live, ограничения адресатов и результаты повторного аудита:
[ORDER_DESK_STAGING_GO_LIVE.md](ORDER_DESK_STAGING_GO_LIVE.md).

## 1. Архитектура

Используется существующий sales core: `InboundMessage(source=website)` →
`DraftOrder(customer_type=retail)` → подтверждение менеджером → `Order`.
`OrderDesk` — метаданные канала (внешний ID, ответственный, Telegram-привязка),
а не самостоятельный заказ. Номер заявки `D` стабилен для переписки; после
подтверждения карточка дополнительно показывает номер заказа `O`.
Команды `/site`, `/reply`, клиентские `/status` и `/message` используют **D**.
Существующие `/order`, `/payment`, `/tracking` используют **O**.

Webhook сохраняет заявку и `DeskMessage(kind=card)` в одной транзакции.
Менеджерский worker отправляет карточку из outbox; webhook не ждёт Telegram,
Gmail или МойСклад. Клиентский worker использует отдельный bot token и dispatcher.
Клиентские ответы/статусы доставляются этим worker, групповые — manager worker.
На каждый bot ID сохраняется существующая PostgreSQL advisory-lock ownership.

## 2. Webhook и документированные возможности

`POST /integrations/tilda/orders`, HTTPS, JSON или form-urlencoded.
Авторизация: `X-Tilda-Secret: <TILDA_WEBHOOK_SECRET>` у серверного адаптера.
Ответы: 202 новая заявка, 200 повтор, 401 неверный секрет, 409 конфликт внешнего
ID с другим нормализованным заказом, 415 неверный Content-Type, 422 структура,
503 отключён канал/отсутствует конфигурация/БД недоступна. Неуспех можно повторять
с тем же устойчивым ID. Изменённый заказ с тем же ID автоматически не перезаписывается.

Официальные источники, проверены 2026-10-09:

- [Tilda Webhook](https://help.tilda.cc/forms/webhook): POST, HTTPS, ответ до
  пяти секунд; при неуспехе две дополнительные попытки с интервалом в минуту.
  Документированы поля формы, `tranid` (уникальный номер заявки), `formid`,
  опции передачи товарных массивов и `externalid`.
- [Telegram deep linking](https://core.telegram.org/bots/features#deep-linking):
  payload до 64 символов из разрешённого алфавита.

Точная структура массива товаров, сумма, скидки и currency зависят от настройки
проекта. Контракт ниже — **контракт адаптера**, а не утверждение о native payload
любой корзины Tilda. Перед подключением нужен синтетический образец из конкретного
проекта и проверка mapping. Тестовый запрос проверки соединения без корзины/ID
получит 422: не подменять его фиктивным заказом.

## 3. Безопасность

`TILDA_ENABLED=false` по умолчанию. Секрет отдельный, случайный, минимум 32 символа.
Без него приём fail-closed. Не передавать secret браузеру, в JS, репозиторий или
общую Telegram-группу. HTTPS обязателен на ingress; публичный API не использует
Telegram ID из браузера для авторизации клиента.

Native Tilda, если не поддерживает нужный заголовок в конкретной настройке:
`https://HOST/integrations/tilda/orders?secret=<opaque-secret>` при
`TILDA_ALLOW_QUERY_SECRET=true`. Приложение удаляет query из ASGI scope до access
logging и не пишет body. **До включения** подавить query logging в Railway edge,
reverse proxy, APM и аналитике. Если это невозможно проверить, оставить query
mode выключенным и использовать доверенный server adapter. Не выводить полный
webhook URL в диагностике. Не включать отправку cookies Tilda.

Технические логи содержат события, локальные ID, actor ID и безопасные коды
ошибок. Контакты/сообщения есть только в БД и авторизованной рабочей группе.
Ограничить доступ к БД, бэкапам, Railway variables и состав группы. Включён
Telegram `protect_content` для исходящих сообщений; это не заменяет контроль
состава группы. Срок хранения PII определяется общей политикой проекта.

## 4. Поля и parsing

Default `TILDA_FIELD_MAP_JSON`:

```json
{"external_id":"tranid","name":"Name","phone":"Phone","email":"Email","comment":"Comments","items":"products","total":"amount","currency":"currency","discount":"discount"}
```

Пути через точку разрешают, например, `payment.products`, `payment.amount`.
JSON-строка в form поле также разбирается. PHP-массивы
`products[0][externalid]`, `products[0][name]`, `products[0][quantity]`,
`products[0][price]` поддерживаются с непрерывными индексами от 0.
Повторяющиеся form keys отвергаются. Item mapping:

```json
{"id":"externalid","name":"name","qty":"quantity","price":"price"}
```

Дополнительные поля игнорируются; контакты и комментарий необязательны.
1–100 позиций, целое количество 1–10000. Деньги парсятся через Decimal,
точность до копейки, без float. Цена/итог неверны или расходятся — заявка
сохраняется в review без доверенного total. Неверная структура/количество — 422.
MVP согласовывает только RUB. Иная currency отмечается для review; подтверждение
означает отдельное согласование **розничной RUB суммы**, не конвертацию валюты.

## 5. Mapping товаров и розничные цены

`TILDA_PRODUCT_MAP_JSON` — оператором проверенный staging snapshot действующих
product IDs и розничных цен из существующего каталога:

```json
{"sku-a":{"product_id":"existing-own-product-id","retail_price_minor":1010},"sku-b":{"product_id":"existing-external-product-id","retail_price_minor":1010}}
```

Это адаптер канала, не новый каталог. Внешний товар может иметь существующий
внутренний ID без физического наличия в МойСклад. Нет сетевого чтения прайса
в webhook, поэтому сохраняется заказ при недоступности каталога/складов.
Нет auto-sync цен; обновляет оператор из проверенного retail price type.
Пустая/неправильная retail price не заменяется wholesale, даже при wholesale
профиле клиента. Отсутствующее сопоставление/цена требуют review.
После исправления mapping redeploy backend/manager worker и `/sitereprice D`.
Команда увеличивает revision и снимает прежнее согласование суммы.

Все Tilda drafts/orders имеют source=website и customer_type=retail.
Найденный wholesale Customer не меняет условия заказа и не изменяется.
Непроверенный контакт не добавляет Telegram identity к чужому customer profile.

Скидки сохраняются как заявленные значения, но не применяются автоматически.
Менеджер явно согласовывает показанную retail сумму с клиентом; кнопка
подтверждения имеет второй шаг. Отдельный редактор скидок отложен.

## 6. Deduplication

Уникальные ограничения БД: `OrderDesk.external_id`, `InboundMessage.external_message_id`,
`DeskMessage.idempotency_key`. PostgreSQL transaction advisory lock на внешний ID
сериализует повторную доставку. Хеш нормализованного заказа обнаруживает конфликт.
Нет дедупликации по одинаковой корзине/контактам. Разные tranid — разные заявки.
Если tranid отсутствует, server adapter обязан передать устойчивый `Idempotency-Key`,
созданный один раз на checkout. Без обоих — 422. Смена ID при каждом retry
создаст дубликаты: это обязательство adapter, а не решаемая backend эвристика.

## 7. Success page и безопасная передача deep link

Native webhook идёт server-to-server. Его JSON-ответ **не считается доступным
success page**. Штатный динамический redirect с токеном не подтверждён.
Без кастомизации заказ остаётся у менеджеров для связи по телефону/email.

Подготовлен точный контракт для доверенного checkout bridge:

1. Bridge создаёт собственную случайную checkout session и связывает её с
   браузером через Secure/HttpOnly/SameSite cookie. Сессия не равна order ID.
2. Bridge передаёт checkout в адаптер и хранит соответствие session → external ID
   → полученный `draft_id` только на сервере. Нельзя доверять draft ID из URL
   браузера или искать заявку по введённому email/телефону.
3. После авторизованного запроса success page bridge вызывает
   `POST /internal/tilda/drafts/D/telegram-link` с `X-Internal-API-Token`.
   Ответ: `deep_link`, `expires_in_seconds`, заголовки no-store/no-referrer.
4. Bridge показывает кнопку «Открыть Telegram» конкретной checkout session.
   Никаких analytics/сторонних скриптов на странице с токеном, не логировать
   response body. Пользователь сам открывает бот и нажимает Start.
5. Для native checkout разработчик должен подтвердить безопасный механизм
   связывания session с webhook конкретного проекта. До этого динамический
   bridge не подключать. Альтернатива: менеджер проверяет контакт и передаёт
   персональную ссылку вручную согласованным каналом; backend сам письма не шлёт.

Bridge/frontend вне этого backend repository **не реализован**; публичного
endpoint выдачи ссылок по угадываемому номеру нет. Internal endpoint предназначен
для доверенного bridge/оператора, не для браузера и не для прямого native Tilda.

## 8. Telegram link и клиент

32 случайных байта → URL-safe токен 43 символа. Хранится только SHA-256,
TTL 30 минут (1–1440, configurable). Повторная выдача отзывает неиспользованные
ссылки. Первый Start атомарно связывает заявку с Telegram actor. Повтор того же
пользователя идемпотентен; другой пользователь не может перепривязать заявку.
Для использованного токена повторный Start владельца допустим после TTL, но
никому другому не даёт доступ. Один клиент может иметь несколько заявок.

Клиент: `/orders`, `/status D`, `/message D текст` (`/manager D текст` — alias).
Свободный текст предлагает явный выбор заявки; это исключает отправку не в тот
заказ. Поддерживается plain text до 3000 UTF-16 units; Telegram entities не
интерпретируются. Вложения получают понятный отказ. Клиент не видит contact
других людей, себестоимость, поставщиков, маржу и внутренние складские данные.
Повторный Telegram message ID не создаёт вторую запись/outbox.

## 9. Railway variables и services

Общие: `APP_ENV=staging`, нужный staging `DATABASE_URL`,
`EXTERNAL_WRITES_ENABLED=false`, существующие разрешения группы и allowlist.
Новая миграция: `p59db643bc76`, additive, без изменения прошлых записей.

Backend: `TILDA_ENABLED=false` до acceptance, сильный `TILDA_WEBHOOK_SECRET`,
`TILDA_ALLOW_QUERY_SECRET=false`, maps, `TILDA_DEFAULT_CURRENCY=RUB`,
`CLIENT_TELEGRAM_BOT_USERNAME`, `CLIENT_TELEGRAM_LINK_TTL_MINUTES=30`,
`INTERNAL_API_TOKEN`, `MANAGER_TELEGRAM_CHAT_ID`, `MANAGER_TELEGRAM_USER_IDS`.

Manager worker: существующий `TELEGRAM_BOT_TOKEN` **не менять**, group config,
тот же `TILDA_PRODUCT_MAP_JSON`, `ORDER_DESK_SEND_ENABLED=false` до acceptance.

Отдельный **client-bot-staging**: config `railway.client-bot.staging.toml`,
start `python -m app.workers.client_bot`, `/health` liveness, `/ready` polling
readiness; token `CLIENT_TELEGRAM_BOT_TOKEN` от отдельного BotFather bot,
`CLIENT_TELEGRAM_ENABLED=true` только при согласованном запуске,
`CLIENT_ORDER_DESK_ENABLED=true`, `ORDER_DESK_SEND_ENABLED=true` только при
разрешённом synthetic Telegram smoke. Group config нужен для очереди сообщений.
Совпадение bot ID с manager отклоняется. Не заполнять manager token новым токеном.
Manager token может быть задан client worker только для проверки различия ID;
client runtime никогда не использует его для polling/отправки.

Не создавать незаполненный live service. Сначала backup, migrate staging,
затем redeploy backend и manager worker, затем настроенный client worker.
Gmail/Buying worker code не менялся; их функции/variables/курсоры сохранить.
В этом цикле Railway services не создаются, remote migrations/redeploy не запускаются.

## 10. Manager assignment

«Взять заказ» проверяет group chat ID, `allowed_event`, allowlist и активный
Manager. Row lock на OrderDesk выбирает ровно одного победителя. Повтор тем же
менеджером успешен; другой видит сохранённого ответственного. Сохраняются Manager
ID, настоящий actor Telegram ID, UTC assigned_at и DeskEvent. Нет скрытого
переназначения. Ответ/согласование/этап/оплата/выполнение доступны ответственному.
Другие разрешённые менеджеры могут читать карточки группы.

## 11. Переписка, outbox и восстановление

Менеджер: `/site D`, кнопка «Ответить клиенту» показывает `/reply D текст`.
`/siteitems D` показывает все позиции с розничными ценами частями без обрезки.
Обычный текст и обычный reply в группе игнорируются. Reply routing по Telegram
reply-message ID не включён: MVP использует разрешённую явную команду с ID.
Сообщение хранит draft/conversation ID (через OrderDesk — order ID), sender type,
actor, source message ID, direction, destination, timestamp, status, idempotency
key, sent Telegram message ID. Перед отправкой всегда DB commit.

Состояния: pending → sending → sent; 429 → pending с retry_after;
Forbidden → blocked; BadRequest/неверная конфигурация → failed;
сетевой timeout/server failure/просроченная lease → uncertain.
SKIP LOCKED защищает конкурентных consumers. Lease 5 минут, send timeout 30 сек.
Client polling продвигает offset только после успешного сохранения update/outbox;
ошибка БД повторяет тот же update. Известные ошибки ввода сохраняются как ответ.

Telegram send и DB commit не атомарны. Timeout не доказывает отсутствие отправки.
Для uncertain проверить историю/контакт с адресатом и выполнить:
`/outbox_sent MESSAGE_ID TELEGRAM_MESSAGE_ID` — доставка подтверждена;
`/outbox_retry MESSAGE_ID` — явно повторить после проверки (дубль возможен).
Эти команды доступны ответственному и аудируются. При blocked сначала связаться
по другому каналу/попросить разблокировать бот. Уведомление о failed клиентском
ответе идёт группе; его содержимое не включает текст переписки в логах.
Диагностика: SELECT id,draft_id,status,attempts,error_code FROM desk_messages;
не выгружать body, токены, контакты в логи. Отправку можно остановить flag без
потери очереди. Retention/очистка audit — отдельная операционная политика.

## 12. Workflow и ручной fulfillment

До подтверждения: new → working (claim) → awaiting_confirmation.
Подтверждение делает существующий Order и связывает его с заявкой. Возможен
awaiting_payment; это контактный этап, не факт оплаты.
Далее существующие `payment_status=unpaid|paid`,
`fulfillment_status=new|assembling|assembled|shipped|cancelled`,
`delivery_status=pending|ready|dispatched|delivered|cancelled`.
Менеджер отмечает оплату существующей проверенной операцией; текст клиента
никогда не меняет оплату. Переходы/actor сохраняются в OrderEvent и DeskEvent;
статусы клиенту — через outbox в транзакции перехода.

Tilda Order имеет `manual_fulfillment=true`: не требует фиктивного контрагента
МойСклад, не создаёт procurement, settlement или shipment при подтверждении,
не резервирует и не списывает остатки. Сборку можно отметить после ручной
проверки наличия. Автоallocation и MoySklad export для этих заказов запрещены
дополнительно к global write guard. Это одинаково для OWN, PARTNER_X и EXTERNAL.
Автоматизация склада/закупок остаётся отдельной будущей задачей.

## 13. Синтетический заказ

```json
{"tranid":"staging-unique-001","Name":"SYNTHETIC Buyer","Phone":"+70000000000","Email":"synthetic@example.invalid","Comments":"Synthetic staging only","currency":"RUB","amount":"30.30","products":[{"externalid":"sku-a","name":"SYNTHETIC A","quantity":"2","price":"10.10"},{"externalid":"sku-b","name":"SYNTHETIC B","quantity":"1","price":"10.10"}]}
```

С тем же payload+ID — один draft и одна new-card event. Новый tranid — новая заявка,
даже если корзина совпадает. Количество 0 → 422; amount=30.31 → сохранён review.
Отправлять только с синтетическим mapping и согласованными Telegram accounts.

## 14. Тесты и E2E acceptance

Unit: `./.venv/Scripts/python.exe -m pytest -q`.
PostgreSQL: существующий `scripts.validate_staging` создаёт isolated schema
`oms_audit_<uuid>`, применяет все миграции и выполняет Alembic check;
`scripts.run_staging_tests` включает `test_postgres_staging.py` и
`test_tilda_postgres.py`. Никогда не подставлять production URL как staging.
Можно использовать локальный PostgreSQL с отдельным портом и synthetic user.
Telegram transport fake тестирует FastAPI → PostgreSQL → aiogram manager/client
dispatch → outbox → payment/assembly/delivery; **это не live E2E Tilda/Telegram**.

Ручной staging smoke после отдельного разрешения на Telegram sends:

1. Проверить false для external writes, отключённые реальные Tilda и email sends,
   head миграции, отдельные bot IDs, активных synthetic managers в allowlist.
2. Согласовать sample payload и product mapping. Отправить пример через adapter.
3. Убедиться: одна заявка/карточка, retail, unpaid, ни одного внешнего документа.
4. Повторить webhook параллельно: новые карточки и заказы не появляются.
5. Через доверенный bridge получить ссылку; без Start заказ остаётся доступен.
6. Нажать Start клиентом, проверить подтверждение и `/orders`. Другой аккаунт
   не может использовать токен или смотреть `/status D`.
7. Два менеджера нажимают «Взять»: один ответственный и одно claim event.
8. Клиент `/message D текст`, ответственный `/reply D текст`; сообщения приходят
   по нужному D, обычный group text не пересылается, повторы updates не дублируют.
9. Подтвердить товары/наличие/сумму; получить O. Отметить ожидание оплаты, paid,
   сборку, собран, выбрать доставку, отгружен, доставлен. Проверить public status.
10. На отдельной заявке проверить отмену; на другом клиенте — блокировку бота,
    failed/blocked notice и контролируемое восстановление.
11. Убедиться в отсутствии MoySklad/email writes, утечек текста/secret в логах.

## 15. Production rollout checklist / ограничения

До production обязательны: native payload acceptance; проверенный checkout bridge
или проверенная ручная выдача ссылки; review retail snapshot и процедуры обновления;
реальный Telegram smoke с двумя менеджерами; подтверждённая защита edge URL logs;
rate limiting ingress и политика anti-abuse; backup/restore; мониторинг failed/
uncertain queue; утверждённые сроки хранения/удаления переписки и PII; отдельное
разрешение на запуск. Текущая версия не объявляет production readiness.

Не реализованы: Tilda frontend/bridge вне repo, attachments, свободные group replies,
переназначение, автоматические скидки/валюты, payment gateway, автоматические складские
документы, автоматическая live-синхронизация прайса. Нет live E2E на реальных сервисах.
Не подключать стандартную Tilda → МойСклад синхронизацию в обход backend.

## 16. Выполненные проверки (2026-10-09)

Финальный единый запуск `pytest` с включёнными isolated PostgreSQL tests:
**509 passed, 0 failed, 0 skipped; дополнительно 7 subtests passed**.
Из них 35 PostgreSQL tests (22 существующих + 13 Tilda/helpdesk).
Использован локальный PostgreSQL 18, отдельный cluster на loopback и новая
`oms_audit_<uuid>` schema; staging/production databases не изменялись.
Все миграции от пустой схемы применены, `alembic check` без новых операций,
единственный head `p59db643bc76`. `compileall`, `pip check`, `git diff --check`
пройдены; проектный secret scan — без находок. Настроенных линтеров в repo нет.
JUnit и migration output сохранены локально в игнорируемой `.staging-artifacts`.

Fake E2E включает webhook → draft/outbox → manager claim → client Start →
двусторонний текст → подтверждение Order → paid → assembled → shipped → delivered.
Telegram transport подменён; проверка менеджеров и транзакции используют реальную
БД. Gmail/Buying regression tests включены. Реальные Telegram/Tilda/MoySklad/
Railway операции не выполнялись, сервисы не создавались и не redeployились.
