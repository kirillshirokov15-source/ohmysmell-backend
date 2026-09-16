> RC extension: manager/client/order operations are described in MANAGER_BOT.md, CLIENT_BOT.md and ORDER_LIFECYCLE.md. The historical acceptance below predates these changes; current RC evidence is appended after staging validation.

# Remote staging acceptance — 2026-09-11

**Результат: staging backend принят для подключения Tilda в режиме заявок
«Цена по запросу → проверка менеджером».** Production, Tilda, Gmail, Telegram
polling и реальные внешние записи не изменялись и не запускались.

Полный сценарий «анонимный retail checkout → оценённый local Order» сейчас
**не принят и не заявляется выполненным**: отсутствует retail price type.
Предусмотренный контракт сохраняет `DraftOrder`, а финализацию без цены блокирует.
Live split на двух складах также не заявляется пройденным: один склад пуст.
Эти ограничения не мешают подключить каталог и приём заявок без оплаты.

## 1–4. Target, revision, health

| Поле | Проверенное значение |
|---|---|
| Public URL | https://ohmysmell-backend-staging-staging.up.railway.app |
| Railway project | `eloquent-wisdom` (`142df0b6-cee3-41a8-9353-ea14ec15f0b0`) |
| Environment | `staging` (`fa04fc09-13d2-4bc4-8f99-486422c7fcb7`) |
| Backend service | `ohmysmell-backend-staging` (`e3b0504a-672a-494e-8110-8769a996324d`) |
| Database service | `Postgres`, только в этом staging environment |
| Branch | `feature/sales-core-v2` |
| Deployed application commit на момент полного E2E | `4505d618f752b40e67ae7a0c02b2ca6dfb3431b6` |
| Successful deployment полного E2E | `f40b41f7-3f2b-4663-b156-4d89cc2e84ca` |
| Initial deployment commit | `54ca4398c5a14948a1e991eaef6cc3d466e95713` |
| Build/start | Dockerfile; Python 3.11; непривилегированный appuser; uvicorn; один API process; без pre-deploy hooks и auto migrations |
| `GET /` | 200; `status=OK`, `message=OhMySmell Backend is running!` |
| `GET /health/db` | 200; `database=connected` |

Commit подтверждён Railway deployment metadata и GitHub feature branch. Этот
отчёт фиксирует проверку application commit; следующий commit добавляет только
сам отчёт. Финальный deployment receipt сохраняется локально в игнорируемом
`.staging-artifacts/remote_final_deployment.json` после проверки соответствия HEAD.

### Аудит и исправление staging environment

В начале отсутствовали `APP_ENV`, MoySklad token и internal token; обычный runtime
использовал development default, checkout был выключен. Настройки исправлены
только в подтверждённом staging service и применены redeploy.

| Variable | Финальное состояние / назначение |
|---|---|
| `DATABASE_URL` | Связь с staging Postgres по private Railway network; значение не раскрывается |
| `APP_ENV` | `staging` |
| `EXTERNAL_WRITES_ENABLED` | `false` |
| `DEBUG_ENDPOINTS_ENABLED` | `false` |
| `PUBLIC_CHECKOUT_ENABLED` | `true`, только staging |
| `INTERNAL_API_TOKEN` | Новый отдельный staging secret, 64 символа; передан через stdin; не в Git |
| `MOYSKLAD_TOKEN` | Настроен из локальной конфигурации; использовался только для GET |
| `MOYSKLAD_WHOLESALE_PRICE_TYPE` | `Цена продажи` |
| `MOYSKLAD_RETAIL_PRICE_TYPE` | Не задан; wholesale fallback запрещён |
| `MOYSKLAD_ORGANIZATION_ID` | ID единственной организации, подтверждённой read-only GET; для чтения не обязателен |
| `MOYSKLAD_WAREHOUSE_IDS` | Два проверенных active store ID; порядок сохранён таким же, как прежняя сортировка default |
| `MOYSKLAD_CATALOG_CACHE_TTL_SECONDS` | `60` |
| `CORS_ORIGINS` | `http://localhost:5500,http://127.0.0.1:5500,https://ohmysmell-staging.example.invalid` |
| Telegram / Gmail / CDEK / Yandex secrets | Не добавлялись; в backend service отсутствуют |
| `STAGING_DATABASE_URL`, `STAGING_INTERNAL_API_TOKEN` | Только для локальных runners; обычному remote API не требуются |
| `PORT`, `APP_NAME`, CA bundle | PORT предоставляет Railway; остальные defaults достаточны; локальный Windows CA path в Linux не переносился |

Переменные проверены через `railway run --no-local` с явными project/environment/
service IDs и скриптом, выводящим только безопасные поля/наличие секретов. Команда
`railway variable list` не использовалась. Dotenv при таком аудите отключён, чтобы
не принять локальный fallback за remote настройку. Credentials fingerprint
совпал со staging URL и отличался от production credentials. Production URL
только разбирался локально для сравнения; соединение с production не открывалось.
SSH execution в контейнере не выполнялся: локальные SSH keys отсутствовали.

## 5. Products API

- `/products`: 200, 401 active products, уникальные backend IDs, валидные поля
  stock/reserve/available и согласованные суммы по складам.
- Все публичные `price`, `price_minor`, `price_major`, `price_type` равны `null`.
  `salePrices` и `available_price_types` в публичный ответ не попадают.
- `/api/v1/catalog`: Pydantic schema проверена; пять страниц с limit=100 покрыли
  все 401 ID без потерь/дубликатов. `requires_review=true`, `price_minor=null`.
- `/openapi.json`: 200, документирует 202 checkout и реальные модели.
- Публичные ответы не содержали секретов, DB URL, traceback или environment dump.

## 6. Internal auth и debug

Без token получен 401 на orders list/details/create/export/allocations,
drafts list/details/finalize, stores, stocks, MoySklad health и email ingestion.
Неверный token также даёт 401. С корректным token list/details, stores, stocks,
health и synthetic ingestion работают; отсутствующие IDs дают 404; неверный
ingestion payload — 422. Пять debug routes дают 404 как с token, так и без него.
Internal token никогда не передавался в публичный browser contract.

## 7–8. Checkout и retail safety

Полный E2E повторён после code deployment. Основной финальный прогон: 60 реальных
HTTP-запросов / 209 проверок. Вместе с supplemental checks: 66 зарегистрированных
запросов / 231 успешная проверка; дополнительно oversized POST и два одновременных
GET. Это функциональная приёмка, не нагрузочный тест.

| Проверка | Remote результат |
|---|---|
| Synthetic website request | 202, `needs_review`, `currency=RUB`, `total_minor=null` |
| Product authority | Backend сам нашёл MARV007 по ID; в draft сохранены реальные ID/name |
| Frontend price / customer_type | Дополнительные поля отклонены 422 |
| Неизвестный product | 409 `stock_unavailable` |
| Quantity сверх остатка | 409 `stock_unavailable` |
| Пустой заказ | 422 |
| Oversized body | 413 `body_too_large` |
| Idempotency replay | Повторный 202 с идентичным response и тем же draft |
| Тот же ключ, другой payload | 409 `idempotency_conflict` |
| Profile resolution | Привязан synthetic customer; `source=website` |
| Existing synthetic wholesale email | Та же customer_id, профиль не переписан; публичная цена по-прежнему null |
| Retail finalize | 400; local Order без retail цены не создаётся |

Ключ хранится в `checkout_requests` без автоматического срока истечения.
PostgreSQL advisory transaction locks и unique keys проверены concurrency tests.
Anonymous email не является аутентификацией оптового клиента.

Финальные persisted данные: retail website draft **9**, fake email draft **10**,
website draft существующего synthetic wholesale customer **11**. Отдельный
защищённый wholesale intake создал local Order **4** (`source=email`) и один
local shipment plan. Цены/итоги Order — integer minor units, суммы строк сходятся.
Это отдельная проверка Order domain, не доказательство retail Order conversion.

Первый прогон сохранил drafts 6/7/8 и local Order 3. Все тестовые контакты
synthetic (`example.invalid`), записи намеренно оставлены для проверки.
После E2E read-only DB проверка подтвердила: website source/retail/null,
единственность inbound message, отсутствие finalized_order_id у retail draft,
`moysklad_order_id`/shipment external_id отсутствуют, `external_operations=0`,
нет внешних delivery IDs. Существующие чужие записи не редактировались.

## 9–10. Fake email и product matching

Локальный `FakeEmailProvider` выдал synthetic envelope с уникальным
external_message_id. Envelope отправлен в реальный защищённый
`/internal/email/messages`: parser → customer resolution → read-only MoySklad
matching → pricing/review → persisted DraftOrder выполнены в remote backend.
Gmail/OAuth не использовались.

| Вход | Результат remote matching |
|---|---|
| `CHNL010` | matched → `02850296-2c07-11f1-0a80-1834003e75e9` |
| `Chanel Allure Home Sport Hair And Body Wash 200 ml` | тот же exact product |
| `MARV007` | matched → `04ddad8e-7ebd-11f1-0a80-1c7900344967` |
| `Marvis Classic Strong Mint 85 ml` | тот же product, нормализовано двуязычное имя |
| `Marvis` | ambiguous, пять candidates, product_id=null; автоматического выбора нет |

Новый email customer имеет тип unknown; контрагент не выбирается автоматически,
draft требует review. Повтор envelope возвращает тот же draft. Authenticated
переключение synthetic draft в wholesale выбирает `Цена продажи`, значения целые,
item_total=price×qty. Неоднозначная строка сохраняет общий total=null и review.
CHNL010 найден в каталоге, но его текущий available=0: matching не обещает stock.

Отдельная read-only проверка с рабочего компьютера: восемь MoySklad GET, 401 product,
два active store, одна организация, один price type (`Цена продажи`). Три страницы
по 200 покрыли весь каталог. Второй cache lookup не сделал нового HTTP-вызова.
Переход provider default pagination через 1000 и защита от повторённой страницы
покрыты automated tests. Real multi-page GET выполнялся с рабочего компьютера,
не из контейнера; remote public pagination проверена отдельно.

## 11. Two-warehouse

`available=max(stock-reserve,0)` проверено для всех remote stock rows; дробная
доступность округляется вниз до целых единиц. Read-only remote stores/stocks
работают; allocation endpoint создал только local planned shipment, без reserve.

| Сценарий | Свидетельство |
|---|---|
| Один склад | Real remote local order/allocation прошли; DB external_id=null |
| Два склада / split | Automated PostgreSQL: 4 единицы распределены 2+2, два local shipment, concurrent retry не дублирует их |
| Частично хватает на первом | Allocation tests забирают остаток с первого и переходят ко второму |
| Недостаточно суммарно / выбран только один | Controlled rejection в tests; excessive checkout также отклонён remote |
| Live remote split | Не выполнен: `5b3c88f0-5505-11f1-0a80-064c0022899f` — 0 доступных product, `ef902edc-c856-11f0-0a80-00b00020eed4` — 367 |

Реальные остатки/резервы не менялись для получения красивого результата теста.

## 12–14. Delivery, write guard, Telegram

CDEK/Yandex/mock payloads и manual courier проверены unit tests. Без credentials
при отдельно проверенном credential gate получается `DeliveryConfigurationError`
до HTTP. В staging раньше него срабатывает `ExternalWritesDisabled`.
Тестовый manual adapter возвращает локальную dispatch reference. Ни CDEK OAuth,
ни доставка/claim, ни реальная курьерская заявка не отправлялись.

Remote `POST /orders/4/moysklad` с корректным token дал 400
`External writes are disabled`. Guard в этом service расположен до DB lookup и
создания client. Изолированный локальный процесс с resolved Railway staging
variables подтвердил девять guard boundaries: customerorder/demand transport,
order/shipment/delivery/external-operation services и три delivery adapter.
Счётчики заблокированного transport: **0 HTTP, 0 DB calls**. Это local execution
с remote config, а не SSH-in-container test. API behavior подтверждён удалённо.
POST/PATCH/DELETE в MoySklad не выполнялись; journal и внешние IDs пусты.

Staging содержит только API и Postgres. Отдельного Telegram service/token нет.
Poller и notification worker не запускались, TelegramConflictError отсутствует.
Draft notifications остаются pending; один ожидаемый warning при fake email
объясняется отсутствующим token. До подключения worker эти synthetic intents
следует обработать отдельно, чтобы позднее не отправить тестовые уведомления.

## 15. CORS / Tilda

Allowed preflight localhost и staging test origin: 200 с точным
`Access-Control-Allow-Origin`; неизвестный origin: 400 без allow-origin.
Credentials не разрешены, wildcard отсутствует. Headers Content-Type и
Idempotency-Key проходят. Тестовый `.invalid` origin не является размещённым сайтом.
Production placeholder остаётся в `.env.example`; final HTTPS origin требуется
перед production. `docs/TILDA.md` обновлён по реальному remote API.

## 16. Performance и instrumentation

Однократные wall-clock измерения Windows workstation → public Railway URL,
включая network/TLS. Cold — первый products после deployment; warm — сразу за ним.

| Операция, финальный application deployment | ms |
|---|---:|
| `GET /` | 917.60 |
| `GET /health/db` | 352.11 |
| `GET /products` cold | 3855.22 |
| `GET /products` warm | 2822.57 |
| Checkout → review draft | 2343.14 |
| Checkout replay | 280.60 |
| Fake email ingestion + matching | 1612.74 |
| Fake email duplicate | 274.88 |
| Wholesale draft review | 383.03 |
| Protected local Order creation | 2324.08 |
| Local allocation | 2737.13 |
| Blocked export | 267.46 |

Concurrent probe: catalog 2652.93 ms, DB health 603.62 ms; health завершился пока
catalog ещё выполнялся. Unit tests также проверяют offloading синхронного
MoySklad I/O. Это свидетельство отсутствия блокировки в проверенном сценарии,
не гарантия поведения под произвольной нагрузкой.

Выявленная проблема observability исправлена: INFO events ранее не попадали в
Railway logs. Теперь app INFO пишет в stdout, root/dependency debug не включается;
добавлен DB health timer с классом ошибки вместо connection exception text.
После redeploy подтверждены cache hit/miss, API durations и DB timer.
В snapshot полного E2E: 18 cache hits, 1 miss, 85 duration fields. DB health внутри
backend — 117.32 ms; median get_stores 650.24 ms, get_stock_by_store 1446.80 ms,
первый get_products 1625.91 ms. Stock обновляется на каждый запрос, cache хранит
только products; это объясняет ненулевой warm time. Оптимизация stock freshness
или пагинации без отдельного обоснования не выполнялась.

## 17–19. Security, migrations, tests

- Responses и Railway log snapshot проверены по известным token/password values,
  включая новый staging token: совпадений нет. Traceback, ERROR messages, 500,
  SQLAlchemy errors, api_failure, TelegramConflictError не обнаружены.
- Railway помечает stderr startup lines Uvicorn как level=error; это четыре
  сообщения `INFO`, не application errors. Ожидаемый notification warning описан выше.
- `.env` и secret artifacts ignored/untracked; reviewable-tree secret scan чистый.
  TLS verification нигде не отключалась: локально использованы доверенные Windows
  CA и Git Schannel. Full DB URL/token не выводились в terminal/report.
- `alembic current`: `i82c4f76ab09` (head). `alembic check`: exit 0, no new upgrade
  operations. Принудительные read-only PostgreSQL session/options; upgrade не запускался.
- `.venv/Scripts/python.exe -m pytest -q`: **174 passed, 6 skipped**, 7 subtests,
  22.19 s. Unit suite запрещает реальные HTTP/DB вызовы.
- PostgreSQL opt-in suite: **6 passed**, 114.53 s, в существующей изолированной
  `oms_audit_776db695703b427d9c8dde461d088a0b` schema staging. Всего **180 tests passed**
  по двум запускам. Checked concurrent finalize/email/checkout, stale callbacks,
  split persistence и review query budget. Никаких production connections.
- `compileall app`, `git diff --check` прошли. После фикса повторён Alembic check.

## 20. New commits

1. `4505d618f752b40e67ae7a0c02b2ca6dfb3431b6` — application/DB metrics, шесть новых
   проверок logging/delivery, актуализация Tilda/operations docs; pushed и deployed
   в staging, полный remote E2E повторён успешно.
2. Documentation-only commit с этим отчётом — не меняет application tree;
   следует за проверенным application commit. `main` не merge и не push.

## 21–22. Known limitations и exact remaining blockers

1. **Retail Order/оплата:** отсутствует отдельный retail price type. Пока допустим
   только null/review intake; wholesale fallback запрещён. Для автоматического
   priced retail Order нужен отдельный согласованный retail прайс и новый E2E.
2. **Live two-store split:** в первом складе available=0. Нужна будущая естественная
   доступность на обоих складах либо отдельный разрешённый test catalog/account;
   текущие реальные остатки не менять ради acceptance. Automated split пройден.
3. **Уведомления:** отдельные staging Telegram token/service отсутствуют; pending
   intents сохранены. Менеджерские API доступны, автоматической доставки уведомлений нет.
4. **Tilda browser acceptance:** сама Tilda не подключалась. Нужны точный staging
   origin её страницы и frontend UX smoke. Backend contract/CORS готовы.
5. **Production:** не проверялась и не разрешена. Rate limiting/anti-bot,
   restricted DB runtime role, backup/restore, migration rehearsal, final origins,
   операционные alerts/worker ownership — отдельные production gates.
6. MoySklad token использовался исключительно для GET; provider-side permission
   scope отдельно не аудировался. Граница no-writes обеспечена staging config,
   guards и transport tests. Packet-level/provider audit log не запрашивался.

Неразрешённых backend/config/code блокеров для **staging приёма заявок без оплаты**
не осталось. Полную безусловную приёмку retail-order/live-split/production этот
отчёт намеренно не утверждает.

## 23. Exact next step for Tilda

Получить точный HTTPS origin тестовой Tilda-страницы; добавить только этот origin
в staging `CORS_ORIGINS`, применить redeploy. В frontend использовать указанный
staging URL, `/api/v1/catalog` и `/api/v1/checkout`, backend product_id, persistent
Idempotency-Key, текст «Цена по запросу» и success по 202. Не передавать internal
token, price или customer_type; не включать оплату. Провести browser smoke:
preflight, пагинация, duplicate submit/retry, 409/422 и мобильный UX. Контракт и
пример находятся в `docs/TILDA.md`. Никаких изменений Tilda в этой задаче не сделано.

## 24. Exact next step for production

Отдельно согласовать production запуск и выполнить `docs/OPERATIONS.md`:
backup/restore и migration rehearsal на копии, final HTTPS origins, strong secrets,
roles, rate limiting, monitoring и процесс manager review. Решить retail pricing
и способ уведомлений. После отдельного разрешения — production read-only smoke
и canary checkout. External writes оставить false; их включение, MoySklad export
и real delivery требуют самостоятельной приёмки и разрешения.

## Локальные evidence artifacts

Игнорируемая `.staging-artifacts/`: `remote_acceptance.json`,
`remote_acceptance_before_logging_fix.json`, `remote_env_audit.json`,
`remote_db_after.json`, `provider_acceptance.json`, `guard_acceptance.json`,
`remote_logs_summary.json`, `remote_metrics.json`, `staging-tests.xml`.
Они содержат отчётные данные; секретные файлы из этой директории не публиковать.
Предыдущий отчёт «blocked before deployment» описывал прежнее состояние доступа
и заменён результатами настоящей приёмки.
