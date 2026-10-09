# Order Desk: staging go-live

## Проверенный исходный снимок, 2026-10-09

Проект `eloquent-wisdom`, только environment `staging`. Три сервиса backend/manager/client
работали на `ddb7d65877d098f6d7132fe8c0569a2e88f560a9`, Alembic `p59db643bc76`.
Read-only аудит подтвердил общую staging БД, различные bot IDs и ожидаемые bot usernames.
`EXTERNAL_WRITES_ENABLED=false`; Order Desk sending и Tilda intake выключены.

В `desk_messages` одна запись: ID 1, `to_customer`, `pending`, attempts 0,
без draft, получатель — разрешённый тестовый аккаунт `898019732`.
Других получателей и состояний sending/uncertain/failed/blocked нет.
32 старых draft notifications уже sent; unnotified Buying events — 0.
OrderDesk — 0, external_operations — 0. Это снимок, перед активацией повторить аудит.

## Ограничение исходящих сообщений

При `APP_ENV=staging` одного `ORDER_DESK_SEND_ENABLED=true` недостаточно.
Нужно одновременное выполнение **двух** условий:

1. Получатель есть в `ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS` (client worker)
   или `ORDER_DESK_STAGING_MANAGER_CHAT_IDS` (manager worker).
2. ID сообщения есть в `ORDER_DESK_STAGING_MESSAGE_IDS` **или** draft ID есть в
   `ORDER_DESK_STAGING_DRAFT_IDS`.

Все значения — comma-separated integer IDs; пустые списки запрещают доставку.
Неправильная конфигурация останавливает worker с безопасной категорией ошибки.
Нельзя использовать wildcard. Отбор применяется до блокировки/изменения строк,
в том числе при переводе просроченного sending в uncertain. Исключённые строки
не меняют статус или attempts. `/outbox_retry` и `/outbox_sent` также проверяют scope.
В остальных окружениях сохранён прежний контракт доставки.

Для первого, отдельно разрешённого smoke достаточно на **client**:

```dotenv
ORDER_DESK_STAGING_CLIENT_RECIPIENT_IDS=898019732
ORDER_DESK_STAGING_MESSAGE_IDS=1
ORDER_DESK_STAGING_DRAFT_IDS=
ORDER_DESK_STAGING_MANAGER_CHAT_IDS=
ORDER_DESK_SEND_ENABLED=true
EXTERNAL_WRITES_ENABLED=false
```

Это разрешит только уже проверенное сообщение 1. Новые `/start`, `/help`, `/orders`
порождают новые сообщения: их IDs нужно сначала проверить и явно добавить в scope.
Нельзя автоматически включать все сообщения разрешённого пользователя.
Для синтетического заказа после intake разрешается только полученный synthetic draft ID.
Это покрывает связанные ответы и уведомления; общие команды без draft требуют message IDs.
После smoke выключить sending. При uncertain не повторять автоматически: сверить Telegram
и только затем выполнить контролируемое reconciliation.

Ограничитель касается Order Desk. Старые Gmail/Buying notification workers остаются
отдельными; manager redeploy перед полной проверкой требует аудита их очередей.

## Диагностика и deployment

Запускать из корня через `.\.venv\Scripts\python.exe`:

```powershell
.\.venv\Scripts\python.exe -m scripts.staging_order_desk_audit
.\.venv\Scripts\python.exe -m scripts.staging_order_desk_deploy --service backend
.\.venv\Scripts\python.exe -m scripts.staging_order_desk_deploy --service client
```

Audit использует Railway owner CLI login, staging PostgreSQL в read-only transaction,
Telegram `getMe`/`getWebhookInfo` и HTTP health. Он не читает body переписки и не печатает
tokens/URL БД/контакты/сторонние Telegram IDs. Результат — игнорируемый
`.staging-artifacts/order-desk-live-audit.json`. SSH probe необязателен: при отсутствии
зарегистрированного SSH key возвращает `ssh_key_unavailable`, не создаёт ключ.

Deploy по умолчанию выводит план. `--execute` разрешён только после проверки плана,
тестов и commit: проверяет UUID проекта/environment/сервиса, ветку и выключенные
external writes/sending/Tilda. Переменные не меняет, миграции не запускает, manager
не обновляет. Git push не нужен, чтобы не инициировать обновление других services.
Upload собирается только из `git archive` проверенного commit; локальные `.env`,
БД, backup и артефакты не попадают в него. Для client конфиг копируется под стандартным
именем `railway.toml`: текущая service не настроена на custom config path. Это изменение
содержимого code upload, без изменения variables или source settings Railway.
Архив upload остаётся в игнорируемой `.staging-artifacts` для проверки.
Результат deployment сохраняется в `.staging-artifacts/order-desk-deploy-ROLE.json`.

Client start command: `python -m app.workers.client_bot`; config
`railway.client-bot.staging.toml`, healthcheck `/health`, timeout 120.
До включения GitHub autodeploy для client отдельно настроить custom config path:
без него последующий deploy из GitHub не прочитает этот TOML. Такое live изменение
настроек в текущий read-only/code-only scope не входит.
`/health` отражает отсутствие отказа процесса, `/ready` дополнительно требует polling.
Во время переключения deployment новый процесс может ожидать advisory lock старого;
поэтому rollout healthcheck использует `/health`, после запуска проверяется worker_ready.
Публичный домен для worker не обязателен. Backend запускается Docker CMD, без Alembic.

`worker_startup_failed` теперь содержит безопасные `reason` и `role`: например
`client_worker_disabled`, `client_bot_token_missing`, `bot_roles_share_identity`,
`manager_group_configuration_invalid`, `invalid_staging_delivery_scope`.
Текст исключения/значения variables не логируются.

## Что требует отдельного подтверждения

Live variables, Telegram sends, создание synthetic staging orders и реальные bot
действия не выполняются в рамках read-only аудита/code-only deployment.
Единственный заранее утверждённый личный получатель — `898019732`.
Рабочая группа менеджеров — отдельный получатель; её участие нужно явно разрешить.
Чужие pending/failed/blocked/uncertain записи нельзя удалять или менять.

Для полного synthetic smoke после подтверждения:

1. Повторить audit и обновить manager код, проверив его независимые очереди.
2. На backend добавить отдельный случайный `TILDA_WEBHOOK_SECRET`, synthetic
   `TILDA_PRODUCT_MAP_JSON`, явные field/item maps, client username и manager group
   configuration; `TILDA_ENABLED=true`, sending оставить false.
3. На client добавить существующие `MANAGER_TELEGRAM_CHAT_ID` и
   `MANAGER_TELEGRAM_USER_IDS` из manager service. На manager — тот же synthetic
   product mapping, что на backend. Bot tokens не переносить и не заменять.
4. Отправить уникальный synthetic webhook и повторить его. Проверить один DraftOrder,
   retail, одну card event и отсутствие external operations. Запрос с неверной суммой
   должен попасть в review. Никаких реальных контактов или товаров покупателей.
5. Проверить адресатов созданных сообщений; установить recipient AND draft scope
   на оба worker, затем включить sending только после отдельного разрешения.
6. Через internal API получить одноразовый link; передать тестеру приватно, не логировать.
   Тестер сам нажимает Start от разрешённого аккаунта. Бот не может нажать Start
   или создать настоящее пользовательское сообщение за человека.
7. Тестер проверяет `/start`, `/help`, `/orders`, `/start TOKEN`, `/status D`,
   `/message D тест`; менеджер берёт карточку, открывает `/site D`, отвечает `/reply D тест`,
   подтверждает заказ и меняет внутренние payment/assembly/delivery statuses.
8. Проверить адресную доставку, actor audit, единственного ответственного, повторные
   webhook/update без дублей. Конкуренция двух менеджеров проверена локально;
   для второго настоящего аккаунта нужно дополнительное разрешение.
9. Выключить sending/Tilda intake после smoke, проверить очередь и отсутствие внешних
   складских операций. Не удалять тестовую историю без отдельного решения.

Исходно на backend отсутствуют Tilda secret/maps, client username и group config;
на client отсутствует group config; на manager отсутствует product map.
Field/item maps имеют defaults, но реальный контракт всё равно требует проверки.
Новые четыре scope variables отсутствуют — это безопасное deny-by-default состояние.

## Точный checklist разработчика Tilda

- Использовать только тестовую страницу, не подключать реальную корзину.
- Endpoint: `https://ohmysmell-backend-staging-staging.up.railway.app/integrations/tilda/orders`.
- Предоставить synthetic native payload с `tranid`, несколькими позициями, количеством,
  ценой, total, currency и external product IDs; сверить field/item mappings.
- Согласовать external product ID → внутренний product ID и retail price minor units.
  Wholesale fallback запрещён; неизвестная цена/расхождение суммы требуют review.
- Server adapter передаёт `X-Tilda-Secret`; secret нельзя размещать в браузере.
  Query secret допустим только после отдельной проверки редактирования edge URL logs.
- Сохранять один устойчивый external ID при retry. Пример synthetic payload приведён
  в [TILDA_ORDER_FLOW.md](TILDA_ORDER_FLOW.md#13-синтетический-заказ).
- Success page получает order-specific link только через доверенный checkout bridge
  с доказательством владения checkout. Native webhook response не считается доступным
  браузеру. До реализации bridge возможна приватная ручная выдача тестового link.
- Не включать Tilda → MoySklad sync, оплату, реальных клиентов или production.

## Проверки кода

Полный запуск: **531 passed, 0 failed, 0 skipped + 7 subtests**; 41 PostgreSQL test.
Изолированная новая схема локального PostgreSQL 18: все миграции и Alembic check.
Fake transport E2E покрывает webhook → draft → manager card/claim → client linking →
двусторонний текст → Order → paid/assembled/shipped/delivered. Настоящие FastAPI,
SQLAlchemy и aiogram handlers, но сеть Telegram заменена. Это не live Telegram/Tilda E2E.
Live staging БД при аудите только читалась; новая миграция для этой доработки не нужна.
