# OHMYSMELL 1.0 — приёмка и готовность выпуска

Дата проверки: 10.10.2026. Ветка: `feature/sales-core-v2`.
Основа: `5398a10ae441d0469523d899b8e4b6bf1eea5f78`.

**Локальная реализация подготовлена к дальнейшей интеграционной приёмке.
Production и фактический staging deployment пока заблокированы.**
Реальные Gmail/MойСклад сквозные проверки не проводились; воспроизводимость
контейнеров отката не доказана. Наличие исходников или кнопки Redeploy не заменяет
проверенную сборку и запуск предыдущей версии.

Production, Railway variables, реальные заявки/заказы не изменялись. Миграций,
deployment, остановки Railway, push, отправки писем, уведомлений и документов
МойСклад не было. `EXTERNAL_WRITES_ENABLED=false` сохранён.

## 1. Реализованные функции и совместимость

- Используются существующие `Shipment`, `WarehouseAllocation`, `OrderEvent`,
  `ExternalOperation` и Buying. Нового frontend или параллельного учёта нет.
- Own / X / External распределение позиции между источниками и складами.
  Одну Buying-закупку можно распределить между несколькими заказами в пределах
  количества конкретного товара/предложения. Закупка не отменяется при отмене
  только одного из использующих её заказов.
- Own: свежий отчёт остатков, локальные блокировки по товару/складу, учёт резервов.
  Подтверждённый реальный резерв не вычитается повторно из свободного остатка;
  неизвестные результаты и симуляции продолжают удерживать локальную потребность.
- X/External: назначение источника, подтверждение физического получения,
  документ поступления, резерв под заказ, сборка, отдельная отгрузка по складу.
  Buying `confirmed` не означает `received`; `received` само по себе не означает
  проведённый документ МойСклад.
- Адаптеры документов `supply`, `customerorder` с резервом и `demand`, снятие
  резерва через обновление существующего документа. Сверяются склад, контрагенты,
  syncId/externalCode, количество, цены и полный состав позиций.
- Неизвестный результат блокирует повторную запись. Явная сверка ищет ровно один
  соответствующий документ/письмо. Несовпадающие параметры не принимаются.
- Отмена плана с неизвестным внешним результатом или активным резервом запрещена.
  После снятия резерва старый ключ не создаёт новый резерв. После проведённого
  поступления отмена распределения требует отдельного складского решения,
  чтобы не оприходовать то же количество второй раз.
- После отгрузки, доставки и оплаты карточка показывает завершение заказа;
  существующие статусы и правила переходов сохранены, нового статуса БД нет.
- Самостоятельная Buying-закупка без распределений проводится целиком на выбранный
  склад через тот же журнал документов. После этого товар используется как
  собственный остаток; эту закупку нельзя повторно оприходовать через External.
  Неизвестный результат также блокирует её распределение до сверки.
- Прайсы XLSX/CSV поступают в существующие импорт, предложения и историю цен.
  Gmail: проверка адреса поставщика и результатов DMARC Google; неоднозначный
  отправитель или SKU не выбирается автоматически. SKU сравнивается с article/code,
  архивные товары исключены. Без совпадения создаётся локальная позиция Buying,
  а не ложная привязка к МойСклад. Цены МойСклад не изменяются.
- SHA-256 ключ аккаунт/message ID/attachment ID исключает повторный импорт.
  Предложения и история разных поставщиков независимы. Ошибки отдельного
  поставщика не блокируют обработку остальных корректных вложений.
- Gmail-адаптер отправки берёт из закупки только наименования и количества.
  Фиксированный Message-ID, durable intent, message/thread ID, явная сверка после
  таймаута. Повторное нажатие не отправляет второе письмо.
- CUSTOMER и SUPPLIER Gmail имеют отдельные credentials, токены, настройки и
  workers. Локальные тесты используют fake Gmail/Telegram/МойСклад без OAuth.
- Роль Picker не получает новые send/reconcile/confirm функции Buying.
  Существующие API-пути, поля и сценарии Tilda/Gmail сохранены. Добавлен статус
  закупки `confirmed` и необязательные поля/API; UI должен учитывать новый статус.

## 2. Проверки

Все тесты выполнены локально; PostgreSQL — в новой изолированной схеме
специального loopback-кластера, без записи в Railway.

| Проверка | Результат / доказательство |
| --- | --- |
| Полный pytest в `.venv` | **669 passed, 66 skipped, 7 subtests passed**, 103.55 s |
| Отдельный PostgreSQL runner | **66 passed**, 27.62 s; opt-in тесты, пропущенные обычным запуском |
| Связанные тесты новых функций после проверки release guards | **16 passed**, 12.17 s |
| Все миграции на новой локальной схеме + alembic check | Passed, без autogenerate изменений |
| Restore предварительного backup | Passed: 44 таблицы, 477 строк, точный fingerprint |
| Копия backup: p59 → q60 → r71 | Passed; значения всех прежних колонок сохранены |
| Копия backup: r71 → p59 → r71 без новых данных | Passed; повторный alembic check passed |
| Read-only staging preflight | p59; external writes false; старые очереди legacy/Buying пусты; защищённые hashes совпали |
| Снимки четырёх предыдущих Railway-сервисов | Архивы исходников проверены по каждому файлу; сборки образов не проверены |

JUnit и локальные доказательства находятся в игнорируемой `.staging-artifacts`:
`ohmysmell-1-unit.xml`, `staging-tests.xml`, `validation.json`,
`ohmysmell-1-migration.json`, `warehouse-release-preflight.json`,
`rollback-artifacts.json`. Они не включаются в Git.

Команды проверки (требуется запущенный проверенный локальный PostgreSQL):

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short
$env:STAGING_DATABASE_URL='postgresql://oms_test@127.0.0.1:55439/postgres'
$env:EXTERNAL_WRITES_ENABLED='false'
.\.venv\Scripts\python.exe -m scripts.validate_staging
if ($LASTEXITCODE -eq 0) { .\.venv\Scripts\python.exe -m scripts.run_staging_tests }
.\.venv\Scripts\python.exe -m scripts.rehearse_ohmysmell_migration .staging-artifacts/backups/warehouse-preliminary-20261010T192200Z-7834423c
```

Не подставлять в тестовую команду настоящий staging URL. Каждому повторному прогону
PostgreSQL должен предшествовать `validate_staging`, создающий новую схему.

Охват: существующие Tilda → CRM → manager/client, Gmail wholesale, розничные и
оптовые цены, проверки ревизий/ролей/повторов, отменённые планы, конкурентные
распределения, недоступность провайдеров; новые Gmail-прайсы/история/дедупликация,
fake письмо и сверка неизвестного результата, подтверждение/получение закупки,
полный заказ на 8 единиц из трёх источников на двух складах, 3 поступления,
2 резерва, 2 отгрузки, доставка и оплата. Тесты не обращаются к реальным API.

В ходе проверки исправлены неверная нормализация local PostgreSQL IP, зависимость
fingerprint от timezone/collation, повторное использование снятого резерва,
несогласованный intent при сверке, двойной учёт реального резерва и возможность
повторного поступления после отмены. Runner теперь требует новую проверенную схему:
повторный прогон в прежней схеме оставлял историю тестовых уведомлений и давал
ложные сбои. Один пробный запуск системным Python завершился ошибками зависимостей;
проект проверяется именно `.venv`, глобальное окружение не изменялось.

## 3. Backup и защищённые данные

Архив (не удалён и не перезаписан):
`.staging-artifacts/backups/warehouse-preliminary-20261010T192200Z-7834423c/staging-before-q60.dump`.

SHA-256:
`3c44d7bb40b2501e97e0f4b03f263eb79097ed48342dbe43f96f62d9b38da29a`.

Первое успешное восстановление: `oms_restore_32128bec3adf45f2abc82db9c0e2fda2`.
Отдельная копия для миграций: `oms_restore_ecd8b9b4c7ef49a69fa4be286c2addde`.
Обе на loopback `127.0.0.1:55439`, а не Railway. Существующие backup не удалялись.
Проверены точные hashes `draft_orders`, `draft_order_items`, `orders`, `order_items`
для №38/№16 и связь OrderDesk. Клиентские данные в отчёт не включены.

Причина первоначального STOP: `inet_server_addr()::text` возвращал
`127.0.0.1/32`. Исправление использует `host(...)`; проверки адреса, порта,
точного data_directory и новой UUID-БД сохранены. Для старого Linux fingerprint
учтены UTC и порядок строк, но принимается только полное совпадение старого SHA.
Новый fingerprint сортирует сериализованные строки независимо от collation.

Это предварительная копия. Для выпуска обязательна новая копия после остановки
всех пишущих процессов и повторная проверка восстановления.

## 4. Миграции и совместимость отката

`q60ec754da87` сохранена. Новая `r71fd865eb98` необходима для:
nullable unique `buying_imports.source_key`, статуса закупки `confirmed`, снятия
ограничения «одна закупка — одно распределение» с индексом на purchase ID.
Существующие строки и внешние идентификаторы не переписываются.

DDL требует кратковременных блокировок. Runner устанавливает lock_timeout 5 секунд
и statement_timeout 60 секунд; ошибка прерывает выпуск. Новый backend требует r71.
Backend и manager обновляются из одного точного коммита. Старые customer email и
client не используют новые функции Buying, но их откат всё равно требует
проверенной предыдущей сборки. Старую версию Buying нельзя безопасно возвращать
после появления нового статуса или shared allocations.

Downgrade r71 блокируется при `confirmed`, shared allocations или Gmail import
history. Downgrade q60 блокируется при managed allocations. Эти проверки нельзя
обходить удалением данных. После начала новых операций предпочтителен forward fix;
восстановление полного backup требует отдельного согласования потери последующих
изменений и окна обслуживания. Архив восстанавливается в новую БД, сравнивается,
затем отдельно согласуется переключение; не использовать pg_restore поверх живой БД.

## 5. Симуляция и подключение реальных операций

Сейчас внешние записи закрыты. `/warehousedoc` в Telegram намеренно использует fake
adapter; API `warehouse-documents` по умолчанию тоже симулирует. Существующие
`simulate-send`, `post_receipt` и fake Buying receipt остаются совместимыми, но
не являются доказательствами реальной отправки/поступления. Реальная сборка/отгрузка
требует записей успешных реальных резервов/документов, а не `simulation:`.

Для МойСклад подготовлены guarded transport и reconciliation, проверенные fake
ответами. Для включения необходимы отдельно разрешённый тестовый контур/организация,
правильные UUID складов, товаров и контрагентов (включая X и поставщиков), полномочия
на документы, контроль фактической стоимости поступления. Для валютной закупки
передать утверждённую стоимость в RUB minor units; приблизительный FX каталога
не используется как подтверждение стоимости документа.

Перед рабочим включением проверить реальные форматы create/read/update ответов,
проведение и снятие резерва, отражение документов в отчёте остатков, видимость после
записи и таймауты. Полная сверка внешних ручных изменений/удаления документов не
автоматизирована: journal подтверждает результат операции, но не заменяет контроль
дальнейших действий в МойСклад. Возвраты/перераспределение после оприходования
автоматически не выполняются. При таких ситуациях операция останавливается.

Для самостоятельной Buying-закупки без клиентского заказа после отметки «Получено»
доступен `POST /buying/purchases/{id}/warehouse-receipt`: `warehouse_id`,
`simulate=true` по умолчанию, `reconcile=false`, необязательные `costs` (точные
копейки RUB по product ID). Закупка проводится целиком на один выбранный склад;
распределённые закупки проводятся только через существующие source plans.
Последующее использование самостоятельного поступления — через Own и свежий
отчёт остатков. Возврат к External той же закупки блокируется, включая неизвестный
результат первой операции. Автоматическое частичное получение закупки не добавлялось:
`received` означает подтверждение всего её состава.

## 6. OAuth двух Gmail

Инструкция: [GMAIL_MAILBOXES.md](docs/GMAIL_MAILBOXES.md), восстановление доступа:
[GMAIL_RECOVERY.md](docs/GMAIL_RECOVERY.md).

От пользователя нужны два разных аккаунта и OAuth client credentials с включённым
Gmail API; согласие CUSTOMER на `gmail.readonly`, SUPPLIER на `gmail.readonly` и
`gmail.send`. Указать разные EXPECTED_EMAIL и отдельные защищённые token/client
файлы вне репозитория. Пароли не нужны. Не публиковать токены и не копировать весь
environment клиентского worker в supplier worker.

Только при участии пользователя, без автоматического запуска:

```powershell
.\.venv\Scripts\python.exe -m app.scripts.customer_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.customer_gmail_smoke_test
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_oauth
.\.venv\Scripts\python.exe -m app.scripts.supplier_gmail_smoke_test
```

Smoke только читает. Отправка первого реального письма требует отдельного разрешения.
Supplier worker отдельный: `python -m app.workers.supplier_email_runtime`,
`railway.supplier-email.staging.toml`. Он ещё не создан в Railway.
Runtime не запускает интерактивную авторизацию; отзыв токена требует восстановления
через документированный OAuth. Сначала проверить реальные образцы XLSX/CSV и
настроить parser каждого поставщика; PDF и другие форматы не обещаются.

## 7. Buying UI: готовое ТЗ для ChatGPT Sites

Frontend находится в отдельном ChatGPT Sites, исходников в backend нет.
Существующий UI не менялся и не публиковался; браузерная сквозная приёмка нового
поведения UI не выполнена. Не создавать второй frontend.

1. Сохранить текущие login/session, URL и каталог/корзину/историю, Manager/Picker.
2. В текущем upload разрешить `.xlsx,.csv`, сохранять существующие preview/confirm
   endpoints. Показывать неоднозначные SKU для ручного выбора, не выбирать первый.
3. Добавить отображение `confirmed` как «Подтверждено поставщиком», между sent и
   received. Received не подписывать «Оприходовано в МойСклад».
4. Manager: после проверки состава закупки кнопка подтверждения отправки вызывает
   `POST /buying/purchases/{id}/send`; при disabled/409 показать запрет отправки.
   Не заменять её автоматическим вызовом при checkout или загрузке карточки.
5. При inflight/uncertain заблокировать отправку и предложить
   `POST /buying/purchases/{id}/reconcile-email`. Ошибка/отсутствие найденного письма
   не разрешает повторную отправку. Показывать message ID/thread ID из текущей модели.
6. Подтверждение поставщика: `POST /buying/purchases/{id}/confirmed`.
   Существующая received-кнопка фиксирует фактическое получение, не отправку.
7. Picker не видит цены, валюты, письма и эти manager endpoints; использовать только
   существующие pickup endpoints с их ограниченными ответами.
8. Для связи с CRM используется необязательный `purchase_id` в существующем SourcePart;
   не создавать новые клиентские заказы или параллельную корзину.
9. Приёмка UI: fake закупка → sent → confirmed → received; неизвестная отправка,
   отказ доступа Picker, disabled API, CSV/Excel и неоднозначный SKU. Без публикации
   и без реального Gmail до отдельного разрешения.
10. Manager: для самостоятельной полученной закупки добавить выбор склада и
    явное подтверждение `warehouse-receipt`. Показывать режим симуляции и ссылку/ID
    результата; при неизвестном исходе только сверка с прежними параметрами.
    Для распределённой закупки предложить текущие складские планы CRM.

## 8. Безопасный выпуск и откат

Автоматизация: `scripts.release_ohmysmell`. Без аргумента execute печатает только план.
Она не выполнялась в режиме execute и не считается проверенным реальным deployment.

Перед выпуском требуется: чистый согласованный commit, проверенная пересборка и
запуск всех четырёх предыдущих контейнеров, согласованное окно, исправный локальный
restore PostgreSQL. В `.staging-artifacts/rollback-artifacts.json` все
`image_rebuild_verified=false`: это текущий блокер, не изменять флаг без проверки.
Для backend найден исходный commit `7425ca8c9ba7638fcc53f4c01046ce2491d9ff5d`,
manager — `2ac83c4f4e6580d210a69f8f37efd0545b0140d`. Client/email не совпали
с доступными git trees; сохранены проверенные архивы runtime source и версии пакетов.
Docker в локальном окружении недоступен. Нужен разрешённый builder для проверки
восстановления именно этих версий, без подмены их текущим HEAD.

Безопасные команды подготовки:

```powershell
git branch --show-current
git status --short
git rev-parse HEAD
.\.venv\Scripts\python.exe -m scripts.warehouse_release_preflight
.\.venv\Scripts\python.exe -m scripts.audit_release_artifacts
.\.venv\Scripts\python.exe -m scripts.release_ohmysmell
```

Порядок после снятия блокеров и **отдельного разрешения на точный commit**:

1. Read-only preflight, доказательство staging endpoint vs production,
   текущие deployment IDs, пустые опасные очереди, неизменные snapshots №38/№16.
2. Проверка локального restore endpoint до остановки. Окно обслуживания: остановить
   customer email, client, manager, backend; дождаться завершения остановки.
3. Повторно проверить Telegram/Buying очереди, flags и защищённые hashes. Очереди
   не чистить и не проигрывать; при старом backlog STOP и ручное решение.
4. Свежий snapshot backup, SHA, restore в новую локальную БД и точный fingerprint.
5. В одной контролируемой операции upgrade до r71 (через q60), alembic check.
6. Backend и manager из одного согласованного git archive. Customer email/client
   восстановить в проверенных прежних версиях; не деплоить туда новый код попутно.
7. Health/readiness backend и manager, polling manager, readonly аудит client,
   readiness customer email worker, queues/flags, revision r71 и неизменные
   №38/№16. Smoke не отправляет уведомления.
8. Supplier worker добавлять отдельно после OAuth и согласования конфигурации.

```powershell
# НЕ ВЫПОЛНЯТЬ до отдельного разрешения и проверки rollback images:
.\.venv\Scripts\python.exe -m scripts.release_ohmysmell --execute --approved-commit <FULL_SHA>
```

Runner сохраняет этапы в `.staging-artifacts/releases/oms1-*/state.json` и
останавливается при исключении. Автоматически повторять частично прошедший выпуск
нельзя. После STOP сначала проверить journal и фактическое состояние Railway/БД.
Автоматического destructive rollback нет. До новых данных возможен проверенный
downgrade и возврат четырёх известных версий; после новых данных — forward fix или
отдельно согласованное восстановление backup с оценкой потери изменений.

Текущий manager allowlist ограничен заявкой №38 и NOT_BEFORE; client sending
отключён. Это сохранено. Новые реальные staging-сценарии потребуют отдельного
согласования allowlist синтетических заявок; старые сообщения не переотправлять.

## 9. Изменения и локальные коммиты

Локальный коммит реализации:
`078c43e971a8f2bbbc9880d005c8cd8a66386337` —
`Complete warehouse documents and supplier Buying workflows`.
Отчёт фиксируется отдельным локальным коммитом
`Document OhMySmell 1.0 release readiness`; его полный SHA:
`git log -1 --format=%H -- OHMYSMELL_1_0_RELEASE_READINESS.md`.

Изменённые/добавленные файлы:

```text
alembic/versions/r71fd865eb98_buying_receipt_imports.py
app/api/buying.py
app/api/supply.py
app/bot/order_sources.py
app/integrations/email/fake_supplier.py
app/integrations/email/supplier_gmail.py
app/integrations/email/supplier_sender.py
app/integrations/moysklad/client.py
app/integrations/moysklad/documents.py
app/integrations/moysklad/payloads.py
app/models/buying.py
app/models/fulfillment.py
app/schemas/buying.py
app/schemas/order_sources.py
app/services/buying.py
app/services/buying_email.py
app/services/buying_excel.py
app/services/external_operation_service.py
app/services/manager_workspace.py
app/services/order_operations.py
app/services/order_source_service.py
app/services/supplier_price_import.py
app/services/warehouse_documents.py
app/workers/supplier_email_runtime.py
docs/GMAIL_MAILBOXES.md
docs/STAGING_WAREHOUSE_RELEASE.md
scripts/audit_release_artifacts.py
scripts/backup_restore_rehearsal.py
scripts/rehearse_ohmysmell_migration.py
scripts/release_ohmysmell.py
scripts/restore_local_backup.py
scripts/run_staging_tests.py
tests/test_ohmysmell_v1.py
tests/test_ohmysmell_v1_postgres.py
tests/test_order_sources.py
tests/test_staging_readiness.py
OHMYSMELL_1_0_RELEASE_READINESS.md
```

`.env`, токены, архивы и `.staging-artifacts` не включаются в commit.
Перед коммитом выполнены `git diff --cached --check` и проверка 36 файлов
реализации на попадание локальных credentials/архивов. Миграция q60 уже содержится
в базовом коммите, r71 включена в новый. Локальный PostgreSQL после проверок
остановлен; его БД и резервные копии сохранены.

## 10. Блокеры и критерии production

Блокеры выпуска: недоказанные rollback images; отсутствие OAuth двух Gmail и
созданного supplier worker; отсутствие разрешённой живой приёмки МойСклад/Gmail;
непройденная приёмка изменений отдельного UI Sites. Известных падений обязательных
локальных тестов после исправлений нет; это не заменяет приёмку реальных интеграций.

Production допускается только после: успешного staging выпуска/отката; проверки
реальных остатков, reserve/release/supply/demand и неизвестных результатов; реального
Gmail прайса, подтверждённого письма и ответа в правильном thread; проверки ролей UI;
утверждённого процесса возвратов/исключений и контроля внешних изменений; свежего
backup с restore; мониторинга workers, OAuth health и очередей; полной сквозной
приёмки на разрешённых тестовых заказах. До этого систему не считать полностью
готовой к production.
