# Order Desk: hardening и план deployment

Изменения подготовлены локально в `feature/sales-core-v2`. Этот документ не является
новым live acceptance. Принятый live baseline — commit `97cfa11`, Draft 36 → Order 15.
Push, deployment, sending, intake и миграции staging в этой работе не выполняются.

## Источники истины

До подтверждения `DraftOrder` хранит проверку позиций/количеств/суммы и отмену;
`OrderDesk.stage` хранит согласование и назначение. После подтверждения authoritative
состояния — `Order.payment_status`, `fulfillment_status`, `delivery_status`.
Поле `Order.status` остаётся частью существующего legacy lifecycle, не заменяет эти оси.

Причина ошибки: Desk-карточка напрямую показывала `OrderDesk.stage`, тогда как
`OrderOperations` менял только Order. Публичная сводка имела собственную частичную
проекцию; дублирование правил позволяло интерфейсам расходиться.

Теперь `desk_status` — общая read-only проекция:

| Состояние | Публичный/Desk этап |
|---|---|
| Неподтверждённая заявка | Получен / В работе / Ожидает подтверждения |
| Отменённая заявка | Отменена |
| Подтверждённый, unpaid/new | Ожидает оплаты |
| paid/new | Оплачен |
| assembling / assembled | В сборке / Собран |
| shipped | Отгружен |
| delivered | Доставлен |
| cancelled/rejected Order | Отменён |

Оплата показывается отдельно, чтобы отгрузка не скрывала unpaid. Сохранённое
`awaiting_payment` старого Desk после подтверждения больше не определяет отображение.
Новая миграция и backfill не нужны; аудит/история не переписываются. Прямые DB-отчёты
должны также читать оси Order, а не считать `order_desks.stage` общим статусом заказа.

Read-only SELECT staging при этой проверке подтвердил: Draft 36 → Order 15,
Desk `awaiting_payment`, Order `paid` / `delivered`. Локальная проекция этих данных
показывает «Доставлен. Оплачен». Код ещё не развёрнут; исторические записи не менялись.

Согласование позиций, финализация Order, аудит и notification теперь используют одну
транзакцию. Ошибка после создания Order откатывает и предварительное одобрение.
Пустая корзина, unresolved match, неподтверждённое количество и отсутствующая/неположительная
цена блокируются до одобрения. Розничная сумма всё ещё требует явного согласования;
заявленные скидки/доставка сайта не становятся доверенными автоматически.

## Outbox

Схема состояний сохранена: pending → sending → sent / blocked / failed / uncertain.
Telegram rate-limit возвращает запись в pending с задержкой. Потеря ответа и истёкший
lease становятся uncertain, без автоматической повторной отправки. Выключенный sending
теперь блокирует и отдельный `run_once`, включая изменение stale sending.
Запоздавший ответ не перезаписывает reconciled запись или результат другой попытки.
Отсутствующий маршрут служебного уведомления менеджеру больше не откатывает сохранение
результата неудачной отправки клиенту; ошибка маршрута записывается безопасной категорией.

В staging нужны recipient allowlist **и** один из вариантов scope:

- явно проверенный message ID;
- разрешённый draft ID **и** `created_at >= ORDER_DESK_STAGING_NOT_BEFORE`.

Новая переменная — ISO-8601 с timezone, например `2026-10-09T20:00:00Z` (пример,
не дата будущей активации). При draft scope без границы worker откажется запускаться
с безопасной категорией `invalid_staging_delivery_scope`. При каждом новом окне
устанавливать новую границу; старые pending включать только поштучно после аудита.
Перезапуск в рамках одного разрешённого окна сохраняет прежнюю границу и очередь.
Recipient restriction действует и для явно выбранного message ID.

**DeskMessage №7:** оставить pending, исключить из message scope. Read-only аудит
подтвердил attempts=0 и отсутствие Telegram receipt. Это устаревший ответ до привязки,
его не нужно отправлять. Он не менялся. При необходимости будущего административного
закрытия нужен отдельный запрос, терминальное состояние/аудит и dry-run; использовать
`sent` без receipt или удалять запись запрещено. Такая операция здесь не добавляется.

## Telegram UX и доступ

Клиент: `/start`, `/help`, `/orders` (10 последних собственных заявок со статусами),
`/status НОМЕР`, `/message НОМЕР текст`. Пустой текст не блокирует durable poller.
Повторный Start показывает текущий заказ и не создаёт повторную привязку.

Менеджер: `/help` с командами и кнопками списков, карточка сайта с проекцией Order,
ответственным, кнопкой read-only истории. История не раскрывает raw audit details и
переписку. Claim скрыт у занятых/закрытых заявок; кнопка ожидания оплаты скрыта после
оплаты. Авторизация группы, allowlist, активный Manager и ownership бизнес-действий
сохранены. Обычный групповой текст не маршрутизируется клиентам.

## Healthcheck: факты и план

Read-only Railway API для `ohmysmell-client-bot-staging` показал:
`healthcheckPath=null`, `healthcheckTimeout=null`, `railwayConfigFile=null`, публичных
доменов нет; start command соответствует client worker. `PORT` явно не задан.
Следовательно, **настроенный платформенный HTTP probe не подтверждён**. SUCCESS и
worker_ready не заменяют проверку HTTP. Live HTTP внутри client worker в этой задаче
недоступен без дополнительного доступа/сетевой настройки, которые не изменялись.

Runtime слушает `0.0.0.0:$PORT`, fallback 8081; неправильный PORT теперь даёт безопасный
`invalid_worker_port`. Реальный локальный HTTP socket test проверяет /health и /ready
при ожидании lock, готовности и отказе. В Dockerfile нет общего HEALTHCHECK: один image
используют backend и workers с разными процессами/портами.

Подготовленные значения для client service:

```json
{
  "startCommand": "python -m app.workers.client_bot",
  "healthcheckPath": "/health",
  "healthcheckTimeout": 120,
  "numReplicas": 1,
  "overlapSeconds": 0,
  "drainingSeconds": 45,
  "restartPolicyType": "ON_FAILURE",
  "restartPolicyMaxRetries": 10
}
```

На client явно согласовать `PORT=8081`, либо проверить назначенный Railway PORT и
соответствие слушающего socket. TOML хранит желаемую конфигурацию, но не доказывает её
применение платформой. Задать service setting `/health` явно и сверить effective
configuration после deployment. [Railway healthchecks](https://docs.railway.com/deployments/healthchecks)
используются при deployment; постоянный мониторинг `/ready` нужен отдельно.
Во время передачи advisory lock /health отвечает 200, /ready — 503. Затем /ready
должен стать 200 и появиться один polling owner. Не использовать /ready как rollout
probe, если новый процесс ждёт освобождения lock старым.

## Безопасный deployment после отдельного разрешения

1. Проверить локальный commit, чистоту дерева, результаты тестов и текущие staging
   flags/очереди. Не изменять сообщение №7 и исторические заказы.
2. Сохранить исходные Variables/настройки. Intake, sending и внешние записи оставить false.
3. Проверить GitHub autodeploy всех services. Email worker слушает feature-ветку;
   обычный push способен перезапустить его. Для изоляции требуется отдельное разрешение
   на временное выключение/восстановление **только autodeploy**, без изменения credentials.
4. Только после разрешения push в feature; обновить backend, manager и client на один
   проверенный commit. Main/production не трогать. Миграции этой доработке не требуются.
5. Применить согласованный client healthcheck и PORT; подтвердить effective probe,
   worker_ready, HTTP readiness и единственного poller. Если нужен SSH/domain для probe,
   согласовать этот доступ отдельно. Не считать отсутствие ошибки доказательством probe.
6. Read-only проверить новую проекцию существующей синтетической заявки 36/Order 15.
   Отдельный Telegram smoke допускается только после разрешения recipients/scope/границы.
7. Сверить очередь и отсутствие внешних операций. Восстановить временные настройки,
   включая email autodeploy; sending/intake оставить выключенными.

Rollback: сначала выключить sending/intake и дождаться применения, затем вернуть
предыдущий image. Не откатывать Order/audit и не включать старый sender на основании
новой временной границы: старый код её не понимает.

Настройка реальной Tilda описана в [TILDA_INTEGRATION_HANDOFF.md](TILDA_INTEGRATION_HANDOFF.md).

## Результаты локальной проверки

- Полный pytest: **602 passed + 7 subtests**, failed/skipped — 0. **55 PostgreSQL-тестов**
  включены в этот запуск и используют отдельную новую схему локального PostgreSQL,
  не Railway DB. Telegram transport в автоматическом E2E подменён.
- Regression lifecycle проверяет весь путь подтверждение → paid → assembling →
  assembled → shipped → delivered при неизменном старом Desk.stage; отмену до оплаты,
  запрет после оплаты, повторы и гонки с устаревшими revision. Проверено отсутствие
  Shipment/ProcurementRequest и новых ExternalOperation для ручного заказа.
- Outbox: старые сообщения/неразрешённые адресаты, повторное открытие окна,
  выключенный sender, lease expiry, неизвестный исход, reconciliation и поздний ответ.
- `compileall app scripts tests alembic`, `pip check`, `git diff --check` — успешно.
- Alembic: один head `p59db643bc76`; fresh upgrade и `alembic check` в изолированной
  локальной схеме прошли. Новых моделей/колонок/миграций нет.
- Secret scan reviewable файлов: находок известных credentials нет. Полные payload,
  тексты переписки и секреты в технические отчёты не включались.

Финальная read-only сверка Railway: backend/manager/client имеют deployment SUCCESS;
на всех трёх intake, sending и external writes выключены. Outbox: customer sent=16,
manager sent=5, customer pending=1 (№7); sending/failed/blocked/uncertain=0.
Сообщений другим адресатам нет. Эта проверка не является новым live Telegram acceptance.
