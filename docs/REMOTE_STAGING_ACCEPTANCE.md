# Remote staging release candidate acceptance

Дата: 2026-09-16. Проверенный application commit: `8faf4d526173aa214489b1599f52286585885b60`.
Последующий documentation checkpoint сохраняет тот же application tree; финальные
local/GitHub/Railway HEAD и health фиксируются в deployment receipt после push.

## Scope and services

Project `eloquent-wisdom`, environment `staging`, branch `feature/sales-core-v2`.

| Service | Назначение |
|---|---|
| ohmysmell-backend-staging | FastAPI; https://ohmysmell-backend-staging-staging.up.railway.app |
| ohmysmell-manager-bot-staging | Отдельный manager poller + notification loop; @OhMySmell_ManagerBot |
| Postgres | Существующая staging DB; private-network reference для worker |

Worker health: https://ohmysmell-manager-bot-staging-staging.up.railway.app/health
Ответ: status=ready, role=manager, environment=staging, polling=true,
external_writes=false. Один replica, один DB advisory lock; второй процесс получил
false от pg_try_advisory_lock до любого getUpdates. FastAPI polling не импортирует.

Production services/DB и main не изменялись. Gmail OAuth, Tilda, платежи, реальные
MoySklad customerorder/shipment и delivery API orders не выполнялись.
EXTERNAL_WRITES_ENABLED=false в обоих сервисах. ExternalOperation и внешние IDs
Order/Shipment пусты. Telegram send/edit разрешены и проверены.

## A — Manager and Order lifecycle

`python -m scripts.release_candidate_e2e` на финальном application tree:

- Fake inbound с MARV007 → draft **22**, реальный read-only product matching.
- Manager classification + заведомо synthetic local counterparty → ready.
- Finalize → local Order **11**, повтор finalize возвращает тот же Order.
- Full local allocation → delivery_method=manual → assembling → assembled →
  shipped → paid. После каждого шага проверены staging DB, revision, immutable total,
  actor/timestamps и число audit events. Записей внешней интеграции нет.
- Повтор каждого действия сохраняет прежний effect/event count; stale action → 409.

Дополнительный тест внутри настоящего Railway worker на application commit `4d037be`
(до добавления ограничений времени ожидания DB в `8faf4d5`):
synthetic draft **18** → Order **8**, реальный Telegram sendMessage/editMessageText,
весь fulfillment/payment flow, пять audit events. Повтор paid не меняет paid_at и
не добавляет event. Проверены email в карточке и полное русское меню.
Acknowledgement callback синтетический; человеческий tap не симулируется как реальный.

## B — Client channel

Fake client transport, отдельный client token отсутствует:
/start → /request → товары/qty → email → /send → draft **23** в реальной staging DB.
Unknown type и отсутствие цены сохраняются; manager HTTP endpoint видит source=telegram.
Повтор update возвращает тот же номер. /status доступен только исходному sender.
Клиентские handlers изолированы; manager callback отклоняется отдельным dispatcher.

PostgreSQL tests дополнительно подтверждают сохранение диалога между экземплярами
service, reuse существующего customer по подтверждённому телефону и отсутствие
identity takeover при введённом вручную контакте. Лишний Customer не создаётся.

## C — API, duplicate/security acceptance

Полный повтор прежнего remote API acceptance на `4d037be`: **60 HTTP requests / 209 assertions**.
Каталог, pagination, stock formula, retail price null без wholesale fallback,
checkout/replay/conflict, input tampering, auth, debug routes и CORS прошли.
Synthetic website draft **19**, email draft **20**, direct Order **10**.
Прямой POST /orders теперь требует Idempotency-Key. External export заблокирован.
OpenAPI snapshot обновлён, order detail содержит fulfillment/payment/delivery/email.

## Performance

Однократные измерения, не load-test и не p95. Worker — реальный Railway процесс
на `4d037be`, настоящие Telegram send/edit, но synthetic callback acknowledgement.
Public API и локальные handler probes повторены на `8faf4d5`.

| Операция | ms |
|---|---:|
| Manager menu, два Telegram send | 390.94 |
| Order list + send | 214.63 |
| Order card + send | 282.06 |
| Начать сборку callback + edit | 343.52 |
| Заказ собран callback + edit | 428.06 |
| Отгружен callback + edit | 277.42 |
| Оплачен callback + edit | 297.73 |
| Read-only catalog внутри worker | 2738.31 |
| Product matching через public staging API | 3800.12 |
| Fulfillment API actions, workstation→Railway | 266–376 |
| Payment API action, workstation→Railway | 272.93 |

Локальный workstation→public PostgreSQL handler probe показал меню 1857 ms,
список 2488 ms, карточку 6369 ms: эти значения включают множество дальних DB trips.
Remote worker использует private DB network и показывает приведённые выше 215–428 ms.
Карточки/статусы не вызывают MoySklad. Stock freshness не ослаблялась ради скорости.

## Database, tests and observability

- `j93d5087bc10`: additive operational fields/audit/client persistence/source.
- Fresh schema upgrade head + alembic check: passed.
- Existing staging upgrade + read-only Alembic check: passed, no model drift.
- Unit: **226 passed**, 11 opt-in PostgreSQL tests пропускаются в unit run;
  дополнительные 7 subtests. PostgreSQL suite отдельно: **11 passed**, 281.89 s.
  Всего **237 passed** в двух наборах, без двойного подсчёта skipped tests.
- compileall, git diff --check и secret scan: passed, credential findings=0.
- Проверены one/two-store split, insufficient stock, partial rejection, concurrent
  finalize/actions/checkout/email, actor permissions, callback expiry/replay/errors.
- Structured startup исправлен для запуска python -m; events идут в app.bot.runtime.
  Lifecycle/action logs содержат IDs/action/duration/result, без email/phone/token.
- Notification queue обработана: pending=0 на remote worker acceptance.
- No TelegramConflictError, traceback, external writes или leaked known secrets в
  проверенных логах. Обычные Uvicorn INFO на stderr Railway помечает level=error;
  это не application failure. Ожидаемый warning API о pending notification до
  обработки отдельным worker не считается провалом сохранения заявки.

Во время работы были временные timeout подключения к публичной DB и один прерванный
локальный прогон, ожидавший сетевого ответа. Добавлены client-side connect timeout
10 s, command timeout 30 s, pool timeout 15 s. Итоговый полный PostgreSQL прогон
прошёл: 11/11. Railway config flags первоначально не применились из-за stdin
priority CLI; исправлено JSON patch, затем verified effective command и health.
Все итоговые сценарии повторены после исправлений.

## Remaining external gates

Отдельный client token; Gmail OAuth/worker; final Tilda origin/CORS/browser flow;
production secrets/infrastructure/backup and controlled activation. Retail price
mapping нужен только для priced retail, review intake уже работает. Первый live
warehouse пуст, поэтому live split не заявлен; автоматический split пройден.
Отдельно остаётся разрешение на будущие внешние writes/delivery/payment integrations.

Evidence (ignored local `.staging-artifacts`): release_candidate_e2e.json,
manager_live_e2e.json, remote_acceptance.json, worker_logs_redacted.jsonl,
remote_logs_summary.json, validation.json, staging-tests.xml, rc_services.json,
rc_final_receipt.json. Secret/key artifacts не публиковать; оба временных SSH public
keys приёмки удалены из Railway, локальные private/public key files также удалены.
