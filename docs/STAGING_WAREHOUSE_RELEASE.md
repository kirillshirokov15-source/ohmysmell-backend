# Выпуск складских источников в staging

> Этот план относится к складскому коммиту `5398a10ae441d0469523d899b8e4b6bf1eea5f78`
> и миграции q60. Для OhMySmell 1.0 необходима также `r71fd865eb98`.
> Актуальные ограничения и результаты: [отчёт 1.0](../OHMYSMELL_1_0_RELEASE_READINESS.md).
> Не запускать новый backend на схеме, остановленной на q60.

План подготовлен 10.10.2026. **Миграции и deployment не запускались.** Команды,
меняющие Railway или staging-БД, ниже предназначены только для отдельного
подтверждённого окна выпуска. Production, Railway variables, заявка №38 и заказ
№16 не изменяются. `EXTERNAL_WRITES_ENABLED=false`; никаких реальных закупок,
поступлений, отгрузок и отправок поставщикам.

## Проверенное состояние

Read-only аудит Railway и PostgreSQL выполнен 10.10.2026 около 18:59 UTC.
Отчёты без паролей и клиентских данных:
`.staging-artifacts/warehouse-release-preflight.json` и
`.staging-artifacts/order-desk-live-audit.json`.

- Ветка `feature/sales-core-v2`, HEAD `2ac83c4f4e6580d2100a69f8f37efd0545b0140d`.
  Новая функциональность **ещё не в commit**: исходно 16 изменённых и 9 новых
  файлов. `git diff --check` прошёл. Полный перечень и назначение файлов —
  [TILDA_SOURCES.md](TILDA_SOURCES.md#проверки-и-изменённые-файлы).
  Проверены модели, миграция, сервис источников, Buying, складское планирование,
  проверки отгрузки, Telegram/API, тесты и runner. Приёмочный отчёт прошлого этапа
  сохранён как исторический, а не как оценка новой версии.
- Принятые результаты: 652 passed / 62 skipped / 7 subtests; отдельно PostgreSQL
  62 passed. Повторять весь Tilda intake, сопоставление и цены для этого выпуска
  не требуется. Изменённая проверка источников уже покрыта новыми тестами.
- Staging БД: `p59db643bc76`; ожидаемые ограничения
  `uq_allocation_shipment_item` и `x_settlements_order_item_id_key` существуют.
  В `warehouse_allocations` 14 строк (вся relation 73 728 байт), в
  `x_settlements` 0 (24 576 байт). Новых столбцов пока нет.
- Backend, manager и client используют одну staging-БД, отличную от production.
  У всех четырёх приложений staging `EXTERNAL_WRITES_ENABLED=false`.
  Backend: `TILDA_TEST_MODE=true`, `TILDA_ENABLED=false`.
- Backend `/`, `/health/db`, `/openapi.json`: 200. Manager `/health`, `/ready`:
  200, polling=true, external_writes=false. Client: те же проверки из контейнера.
- Отправленные сообщения заявки №38: №23/24. Они не подлежат повторной отправке.
  Сохранены SHA-256 исходных строк draft/orders и их items для проверки неизменности.

Это проверка совместимости фактической схемы, а не уже выполненная миграция
staging. Время блокировок на staging и восстановление будущего backup ещё не
измерены. Старый backup не заменяет свежий backup перед выпуском.

## Сервисы

Project `142df0b6-cee3-41a8-9353-ea14ec15f0b0` (`eloquent-wisdom`),
environment `fa04fc09-13d2-4bc4-8f99-486422c7fcb7` (`staging`).

| Сервис | ID | Решение |
|---|---|---|
| ohmysmell-backend-staging | e3b0504a-672a-494e-8110-8769a996324d | Обновить: source API, ORM, Buying API, проверки операций |
| ohmysmell-manager-bot-staging | 7aeac926-0277-4d2f-9765-89c3848858af | Обновить тем же commit: команды, карточки, ORM и операции |
| ohmysmell-client-bot-staging | bdb9fb43-9446-491f-bea3-ee0879d9b831 | Код не обновлять; новые источники не назначает. Отправку оставить false |
| ohmysmell-email-worker-staging | 56306c0e-ff3a-4b34-89e7-856c27d76da5 | Код не обновлять; отдельный intake worker, цены Gmail не менялись |
| Postgres | ba3da870-ebb7-4ef1-b352-f1a4293eb467 | Только согласованная миграция, не redeploy БД |

Отдельного Railway Buying-сервиса в этом окружении нет. Buying router включён в
`app.main`; изменённый helper создания/отмены закупки выпускается с backend и
manager. Отдельный frontend Buying этим изменением не затронут.

Точки восстановления текущих deployment:

| Сервис | Deployment ID |
|---|---|
| backend | 17801175-b1df-46d9-88ed-9b9de8601590 |
| manager | f62f536b-c03d-43f7-8899-633e9a51c882 |
| client | 14834a5e-ddf1-4a0f-b5a7-80764933e322 |
| email worker | 1159d2c0-1f01-4560-a268-7ebe91fb12cd |

У backend/manager metadata не содержит commitHash: **HEAD нельзя считать их
предыдущей версией**. До начала окна проверить доступность Rollback/Redeploy
именно этих deployment в Railway. При отсутствии сохранённого образа/исходников
выпуск остановить до подготовки воспроизводимого артефакта отката.

## Миграция и совместимость

`q60ec754da87` имеет единственного предка `p59db643bc76`. Новые таблицы не
создаются. Добавляются поля источника, состояния получения и связи с закупкой
в allocations; связь allocation в XSettlement. Для старых строк defaults:
managed_source=false, source_type=own, supply_status=confirmed,
receipt_simulated=false; nullable связи остаются NULL. Существующие значения
цены, количества, идентификаторов и финансовых снимков не переписываются.

Прежний unique всей пары plan/item заменён частичным unique для legacy-строк.
Прежний unique X по item заменён частичным для строк без allocation_id плюс
unique по allocation_id. Это необходимо для разделения одной позиции на части.
Существующие unique уже исключают дубли, мешающие новым частичным индексам.
FK новых nullable полей не требуют backfill. На локальной синтетической БД
проверено сохранение прежних колонок 43 таблиц, включая 25 заказов.

| Код / схема | Совместимость |
|---|---|
| Новый код / p59 | Нельзя: ORM запрашивает отсутствующие колонки |
| Старый код / q60, новых назначений нет | Прежние колонки и defaults сохранены; допустимо короткое техническое окно без операций |
| Старый backend/manager / q60 с новыми назначениями | Нельзя разрешать операции: старый код не понимает разделение, отмену и подтверждение источников |
| Новый код / q60 | Целевая комбинация |

DDL берёт блокировки таблиц; создание unique/check/FK не CONCURRENTLY. Текущий
объём мал, но длительные транзакции могут задержать DDL. Выполнять одной
транзакцией, lock_timeout=5s, statement_timeout=60s. При таймауте откатить
транзакцию, не повторять автоматически, проверить `alembic_version` и блокировки.
Не выполнять `stamp`, не удалять ограничения вручную.

Downgrade запрещён, если существует хотя бы одно managed_source назначение,
**включая отменённое**. Их удаление ради обхода защиты недопустимо. Даже при
нулевом количестве назначений сначала остановить новые сервисы: после удаления
столбцов они несовместимы со схемой p59.

## Telegram: условия отсутствия повторных отправок

Текущее состояние: legacy notifications 32 sent, несентированных 0;
Buying events без notified_at — 0; manager desk messages 7 sent, pending нет.
Client имеет 1 pending (№7), но `ORDER_DESK_SEND_ENABLED=false`.

У manager sending=true, draft scope **38**, message scope пуст,
not_before=`2026-10-10T00:00:00+03:00`. `DeskDeliveryPolicy` дополнительно
проверяет разрешённого получателя. `sent` не выбирается; `sending` при истечении
lease превращается в uncertain, а не отправляется повторно автоматически.
Не менять scope, статусы, attempts/notified_at, не вызывать `/outbox_retry`.
Не отправлять команды старым карточкам №38/№16. Новые smoke-заявки не попадают
в draft scope 38; проверять их непосредственно через команды/API.

**Ограничение:** `app.workers.notifications` и `buying_notifications` не используют
DeskDeliveryPolicy. Manager запускает их независимо от ORDER_DESK_SEND_ENABLED.
Поэтому allowlist и EXTERNAL_WRITES_ENABLED=false сами по себе не гарантируют
отсутствие старой отправки. Условие перезапуска: обе legacy/Buying очереди пусты,
нет in-flight отправки; на время снимка/миграции остановлены producer workers.
Если очередь изменилась — STOP, не чистить её SQL и не запускать sender для
«дренирования». Требуется отдельное решение о доставке/карантине.
Новые Buying-события smoke могут породить новые сообщения в настроенную группу;
это реальные Telegram-сообщения, хотя складские операции симулированы. Запуск
таких smoke с работающим manager допускается только при явном согласовании
этих новых уведомлений. Иначе выполнить весь изменяющий smoke локально с fake
Telegram; на Railway оставить только read-only smoke.

## Порядок выпуска

### 0. Зафиксировать точный артефакт

Не выполнять deployment из текущего dirty tree: потеряются незакоммиченные
модули/миграция либо в upload попадут `.env` и артефакты. После review локально
закоммитить перечисленные файлы, **без push**; проверить новый commit и чистое
дерево. В этой подготовке commit не создавался.

```powershell
git branch --show-current
git status --short
git diff --check
git diff --stat
git ls-files --others --exclude-standard
$releaseCommit = (git rev-parse HEAD).Trim() # только ПОСЛЕ commit всех файлов выпуска
git cat-file -e "${releaseCommit}:alembic/versions/q60ec754da87_tilda_source_allocations.py"
if ($LASTEXITCODE -ne 0) { throw 'Migration missing in release commit' }
if (git status --porcelain) { throw 'Working tree is not clean' }
```

### 1. Preflight и окно обслуживания

```powershell
.\.venv\Scripts\python.exe -m scripts.warehouse_release_preflight
if ($LASTEXITCODE -ne 0) { throw 'Preflight failed' }
Copy-Item .staging-artifacts/warehouse-release-preflight.json .staging-artifacts/warehouse-release-before.json
```

Проверить revision=p59, flags и deployment ID по отчёту; сохранить исходный
снимок до любых повторных вызовов. При изменении состава сервисов пересмотреть
план. Убедиться, что Railway не настроен запускать миграцию автоматически
дополнительно: Dockerfile её не запускает, scripts в Docker image не копируются.

После отдельного подтверждения окна: прекратить пользовательские операции,
остановить четыре app deployment (не Postgres). Это исключает фоновые producers,
старые бизнес-операции и polling во время смены схемы. Команда `down` удаляет
deployment, а не данные/сервис; восстановление заранее проверить по сохранённым
deployment ID. Если удаление deployment не согласовано — выпуск не начинать.

```powershell
$projectId = '142df0b6-cee3-41a8-9353-ea14ec15f0b0'
$stageId = 'fa04fc09-13d2-4bc4-8f99-486422c7fcb7'
$backendId = 'e3b0504a-672a-494e-8110-8769a996324d'
$managerId = '7aeac926-0277-4d2f-9765-89c3848858af'
$clientId = 'bdb9fb43-9446-491f-bea3-ee0879d9b831'
$emailId = '56306c0e-ff3a-4b34-89e7-856c27d76da5'
foreach ($serviceId in @($emailId,$clientId,$managerId,$backendId)) {
    railway.cmd down --project $projectId --environment $stageId --service $serviceId --yes
    if ($LASTEXITCODE -ne 0) { throw 'Stop failed' }
}
```

Дождаться завершения процессов, отсутствия sender in-flight и открытых
приложенческих транзакций. Повторить read-only preflight: пустые legacy/Buying
очереди, manager pending=0, защищённые hashes совпадают. Для read-only отчёта
не требуется работающий backend, используются Railway variables и БД.

### 2. Свежий backup и проверка восстановления

PowerShell here-string выполняется локально. URL/пароль не передаются аргументом
процесса и не выводятся. Используется существующий pg_dump helper; main старого
backup_restore_rehearsal **не запускать** — он создаёт БД на staging-сервере.

```powershell
@'
import json
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from scripts.backup_restore_rehearsal import backup
v = dotenv_values('.env')
url, prod = make_url(v['STAGING_DATABASE_URL']), make_url(v['DATABASE_URL'])
assert (url.username,url.password,url.database) != (prod.username,prod.password,prod.database)
p = Path('.staging-artifacts/backups/warehouse-q60-before.dump')
assert not p.exists(), 'Do not overwrite previous backup'
p.parent.mkdir(parents=True, exist_ok=True)
before = backup(url, p)
p.with_suffix('.fingerprint.json').write_text(json.dumps(before,sort_keys=True),encoding='utf-8')
print('Backup and consistent fingerprint saved')
'@ | .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Backup failed' }
Get-FileHash .staging-artifacts/backups/warehouse-q60-before.dump -Algorithm SHA256
& 'C:\Program Files\PostgreSQL\18\bin\pg_restore.exe' --list .staging-artifacts/backups/warehouse-q60-before.dump
```

Список архива не доказывает восстановимость. До миграции восстановить архив в
**новую локальную БД**, без worker/bot, сравнить fingerprint. Команды для ранее
созданного изолированного кластера (127.0.0.1:55439); при отсутствии кластера
сначала подготовить локальный тестовый PostgreSQL, не подменять его Railway URL.

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\pg_ctl.exe' -D "$PWD/.staging-artifacts/acceptance-pg-20261010" -w start
& 'C:\Program Files\PostgreSQL\18\bin\createdb.exe' -h 127.0.0.1 -p 55439 -U oms_test warehouse_q60_restore
if ($LASTEXITCODE -ne 0) { throw 'Restore DB must be newly created' }
& 'C:\Program Files\PostgreSQL\18\bin\pg_restore.exe' -h 127.0.0.1 -p 55439 -U oms_test -d warehouse_q60_restore --no-owner --no-acl --exit-on-error .staging-artifacts/backups/warehouse-q60-before.dump
if ($LASTEXITCODE -ne 0) { throw 'Restore failed' }
@'
import json
from pathlib import Path
from scripts.backup_restore_rehearsal import engine_for, fingerprint
e=engine_for('postgresql://oms_test@127.0.0.1:55439/warehouse_q60_restore')
with e.connect() as c:
    assert json.loads(json.dumps(fingerprint(c)))==json.loads(Path('.staging-artifacts/backups/warehouse-q60-before.fingerprint.json').read_text())
e.dispose()
print('Restored fingerprint matches')
'@ | .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Fingerprint mismatch' }
```

Архив содержит реальные staging-данные: не commit/upload, не публиковать логи
строк. Локальный восстановленный экземпляр не подключать к Telegram/Gmail.

### 3. Миграция — только после подтверждения

Запускать из зафиксированного release checkout. Конкретная revision, не `head`.
Соединение берётся исключительно из STAGING_DATABASE_URL и сверяется с
предварительным аудитом Railway. Весь DDL и Alembic check в одной транзакции.

```powershell
@'
import json
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT
v=dotenv_values('.env')
u,p=make_url(v['STAGING_DATABASE_URL']),make_url(v['DATABASE_URL'])
assert (u.username,u.password,u.database)!=(p.username,p.password,p.database)
live=json.loads(cli('variable','list','--project',PROJECT,'--environment',ENVIRONMENT,'--service','e3b0504a-672a-494e-8110-8769a996324d','--json'))
assert live.get('APP_ENV')=='staging' and live.get('EXTERNAL_WRITES_ENABLED')=='false'
target=make_url(live['DATABASE_URL'])
assert (u.username,u.password,u.database)==(target.username,target.password,target.database)
e=create_engine(u,connect_args={'connect_timeout':10})
with e.begin() as c:
    c.execute(text("SET LOCAL lock_timeout='5s'"))
    c.execute(text("SET LOCAL statement_timeout='60s'"))
    assert c.execute(text('SELECT version_num FROM alembic_version')).scalars().all()==['p59db643bc76']
    cfg=Config('alembic.ini'); cfg.attributes['connection']=c
    command.upgrade(cfg,'q60ec754da87')
    command.check(cfg)
    assert c.scalar(text('SELECT version_num FROM alembic_version'))=='q60ec754da87'
e.dispose()
print('q60 committed')
'@ | .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Migration failed: do not deploy' }
.\.venv\Scripts\python.exe -m scripts.warehouse_release_preflight
```

Проверить: revision=q60; строки allocations=14, X=0; защищённые hashes №38/№16
совпадают. Все прежние allocations managed_source=false. Новые индексы и поля
соответствуют модели. При несовпадении STOP, сохранить диагностику без данных.

### 4. Deployment двух сервисов

Получить чистый upload из одного commit через уже существующий prepare_upload.
Не запускать main `staging_order_desk_deploy --execute`: он не поддерживает
manager и его старые preconditions не соответствуют текущему sending=true.

```powershell
$uploadRoot = (& .\.venv\Scripts\python.exe -c "import subprocess; from scripts.staging_order_desk_deploy import prepare_upload; print(prepare_upload(subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'backend'))").Trim()
if ($LASTEXITCODE -ne 0) { throw 'Archive failed' }
railway.cmd up $uploadRoot --path-as-root --no-gitignore --project $projectId --environment $stageId --service $backendId --detach --message "warehouse-q60 $releaseCommit"
if ($LASTEXITCODE -ne 0) { throw 'Backend upload failed' }
railway.cmd deployment list --service $backendId --environment $stageId --limit 3 --json
```

Дождаться SUCCESS и health backend. Затем тем же upload:

```powershell
railway.cmd up $uploadRoot --path-as-root --no-gitignore --project $projectId --environment $stageId --service $managerId --detach --message "warehouse-q60 $releaseCommit"
if ($LASTEXITCODE -ne 0) { throw 'Manager upload failed' }
railway.cmd deployment list --service $managerId --environment $stageId --limit 3 --json
```

Manager должен сохранить существующий start override
`python -m app.workers.manager_bot` и healthcheck `/health`; иначе STOP. Backend
использует Dockerfile CMD uvicorn. Никакие variables, start commands и service
sources этим планом не меняются. `--detach` означает только принятие upload,
не успешный выпуск.

После обеих проверок восстановить client/email **из их сохранённых deployment**
через Railway Deployments → выбранный ID → Redeploy/Rollback. Не Deploy Latest
Commit и не `--from-source`: их код не входит в этот выпуск. Убедиться, что
client отправка осталась false, email стартует прежним email_runtime.

### 5. Health/readiness и smoke

```powershell
$backendUrl = 'https://ohmysmell-backend-staging-staging.up.railway.app'
$managerUrl = 'https://ohmysmell-manager-bot-staging-staging.up.railway.app'
Invoke-RestMethod "$backendUrl/health"
Invoke-RestMethod "$backendUrl/ready"
Invoke-RestMethod "$backendUrl/health/db"
$spec = Invoke-RestMethod "$backendUrl/openapi.json"
if (-not $spec.paths.'/internal/supply/orders/{order_id}/sources') { throw 'Source API missing' }
Invoke-RestMethod "$managerUrl/health"
Invoke-RestMethod "$managerUrl/ready"
.\.venv\Scripts\python.exe -m scripts.staging_order_desk_audit
.\.venv\Scripts\python.exe -m scripts.warehouse_release_preflight
```

У manager polling=true, environment=staging, external_writes=false; одного
`/health=200` недостаточно. Не должно быть TelegramConflictError,
worker_startup_failed/ownership_lost, UndefinedColumn, MultipleResultsFound,
неожиданных supplier email/MS операций. Сравнить до/после hashes защищённых
строк и IDs/attempts старых сообщений. У backend readiness проверяет БД, но не
всю схему — наличие revision и source route проверяются отдельно.

Warehouse smoke проводится только на **новом отдельном синтетическом заказе**
с ответственным тестовым менеджером и известными item IDs. Если такой заказ
не подготовлен, не использовать №16 вместо него. Создание fixture — отдельная
контролируемая операция, не повторная приёмка webhook/цен Tilda. Сначала
read-only `/sources НОВЫЙ_ЗАКАЗ` и GET source API; команды ниже — шаблон с
реальными ID fixture и текущей ревизией из каждого ответа:

```text
/sources ORDER
/sourceadd ORDER REV ITEM 2 own WAREHOUSE_A
/sourceadd ORDER REV ITEM 1 partner_x WAREHOUSE_B SUPPLIER_X BASE_MINOR
/sourceadd ORDER REV ITEM 3 external WAREHOUSE_A OFFER_ID
/sourceaction ORDER REV X_ALLOCATION receive
/sourceaction ORDER REV X_ALLOCATION post_receipt
```

Для внешнего назначения использовать именно Buying
`POST /buying/purchases/PURCHASE_ID/simulate-send`, затем
`POST /buying/purchases/PURCHASE_ID/received` с существующей Buying-сессией,
после чего два sourceaction receive/post_receipt. Префикс API уточнить в
OpenAPI (router текущей версии использует `/buying`). Не нажимать live email
send и не вызывать экспорт документов. В UI, если нет выбора симуляции,
использовать этот endpoint, а не обычную отправку.

Проверить последовательно:

1. OWN подтверждается только при достоверном свободном остатке, занимает
   локальное количество. X/external пока блокируют сборку/отгрузку.
2. Buying draft и sent не дают готовности; received также не заменяет
   отдельное подтверждённое оприходование назначения.
3. После receive/post_receipt все 6 шт. обеспечены; источники и simulation
   видны в карточке, локальная сборка/отгрузка разрешены при прочих условиях.
4. Повтор одного API idempotency_key не создаёт второй allocation/закупку;
   устаревшая revision отклоняется. Отменённый план не даёт готовности и
   его старые количества не оживают при новом назначении.

Для проверки изменяющего полного сценария без новых реальных Telegram-сообщений
использовать локальный изолированный runner с fake Telegram и поставщиками.
Это уже принятый тест; повторять его только если нужен smoke точного release
артефакта, а не весь набор Tilda:

```powershell
$env:STAGING_DATABASE_URL='postgresql://oms_test@127.0.0.1:55439/postgres'
$env:DATABASE_URL='postgresql://unused:unused@127.0.0.1:1/never_connect'
$env:EXTERNAL_WRITES_ENABLED='false'
.\.venv\Scripts\python.exe -m scripts.validate_staging
if ($LASTEXITCODE -ne 0) { throw 'Isolated validation failed' }
.\.venv\Scripts\python.exe -m scripts.run_staging_tests -k 'split_sources_postgres_concurrency or two_orders_cannot_claim_same_available_stock or automatic_planner_honors_tilda_assignments or buying_send_race_with_source_cancellation'
```

Этот блок выполнять в **отдельном локальном PowerShell**, чтобы process env
не повлиял на последующие release-команды. Не запускать pytest против public
staging и не менять Railway variables для smoke. Совместимость live read-only
catalog готова; записи MS, реальные receipt/export и сверка остатков не входят
в выпуск. Симулированная отгрузка не списывает MS stock.

## Откат

1. **Миграция не закоммичена:** транзакционный rollback сохраняет p59; проверить
   revision, вернуть прежние app deployment. Не stamp и не повторять вслепую.
2. **q60 применена, новых назначений нет:** предпочтительно вернуть старые
   backend/manager, оставив совместимые добавочные поля q60, и запретить новые
   складские операции до исправления. Если нужен полный возврат схемы —
   остановить все приложения и выполнить downgrade ниже. Старые сервисы
   запускать после успешной проверки p59.
3. **Есть managed_source, даже cancelled:** старую бизнес-логику не включать,
   downgrade не запускать. Остановить операции, сохранить дополнительный backup
   текущего q60, предпочесть исправление вперёд. Для полного восстановления
   pre-release состояния требуется отдельное согласование потери всех изменений
   после backup, включая закупки, аудит, очереди и sequences. Сначала restore
   в новую изолированную БД и сверка; затем отдельный согласованный cutover.
   При нынешнем запрете менять Railway variables такой cutover не разрешён.
   Универсальная destructive restore-команда в рабочую БД намеренно не входит
   в этот выпуск. Не копировать старые очереди в работающую систему: возможны
   повторные Telegram-отправки даже после восстановления БД.

Downgrade-команда для **ветки 2**, из release checkout и с остановленными
приложениями; защищённые hashes и свежий backup обязательны:

```powershell
@'
from dotenv import dotenv_values
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
v=dotenv_values('.env'); u=make_url(v['STAGING_DATABASE_URL']); p=make_url(v['DATABASE_URL'])
assert (u.username,u.password,u.database)!=(p.username,p.password,p.database)
e=create_engine(u,connect_args={'connect_timeout':10})
with e.begin() as c:
    c.execute(text("SET LOCAL lock_timeout='5s'")); c.execute(text("SET LOCAL statement_timeout='60s'"))
    assert c.scalar(text('SELECT version_num FROM alembic_version'))=='q60ec754da87'
    assert c.scalar(text('SELECT count(*) FROM warehouse_allocations WHERE managed_source'))==0
    assert c.scalar(text('SELECT count(*) FROM x_settlements WHERE allocation_id IS NOT NULL'))==0
    cfg=Config('alembic.ini'); cfg.attributes['connection']=c
    command.downgrade(cfg,'p59db643bc76')
e.dispose()
'@ | .\.venv\Scripts\python.exe -
if ($LASTEXITCODE -ne 0) { throw 'Downgrade failed: keep apps stopped' }
```

Rollback образа делать через меню **нужного предыдущего deployment**, проверив
его доступность; `railway redeploy` без выбора версии разворачивает последнюю
версию и не является откатом. Railway ограничивает доступность старых образов
сроком хранения. [Railway deployment actions](https://docs.railway.com/deployments/deployment-actions),
[Railway rollback guide](https://docs.railway.com/guides/roll-back-bad-deploy).

## Критерии выпуска / STOP

Успех: чистый зафиксированный commit у backend и manager; q60 и Alembic check;
свежий проверенный backup; оба сервиса ready; источник/Buying отображаются;
неполученные и отменённые источники блокируют отгрузку; внешние записи false;
старые уведомления не повторены; hashes №38/№16 неизменны; client/email
возвращены на прежние версии. Production не затронут.

STOP: неизвестный предыдущий артефакт, ненулевые legacy/Buying backlog перед
перезапуском, изменение scope/flags, DB drift, таймаут DDL, несовпадение hashes,
ошибка health/readiness, попытка реальной закупки/экспорта или смешанная версия
backend/manager при открытых складских операциях.

В ходе подготовки добавлены только этот план и read-only
`scripts/warehouse_release_preflight.py`. Скрипт успешно выполнен против staging;
проверены его компиляция и `git diff --check`. Бизнес-тесты не перезапускались,
их принятые результаты сохранены. SQL подключения preflight принудительно
read-only; protected rows выводятся только как hashes.

Сейчас подготовка позволяет согласовать окно выпуска, но **ещё не разрешает
его запуск**: нужны отдельное подтверждение, commit, проверка доступности
артефактов отката, свежий backup/restore и повторный preflight непосредственно
перед окном. Новая бизнес-функциональность в ходе этой подготовки не менялась.
