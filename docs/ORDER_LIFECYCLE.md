# Order lifecycle

Technical `Order.status` сохранён отдельно от операций менеджера.

Тип заказа определяется каналом: `email → wholesale`, `website → retail`.
Тип существующего Customer — отдельный профиль, он не переопределяет это правило
и не меняется молча. Snapshot `contact_details.customer_type_policy` сохраняет
source/profile_type/order_type/profile_conflict; конфликт виден менеджеру и в логах
создания draft. Для прочих каналов сохраняется прежняя логика профиля/review.
Подмена типа через старый callback или internal draft endpoint блокируется в сервисе
и под DB lock. Review старого незакрытого draft применяет новое правило и пересчитывает
цены атомарно, увеличивая revision при изменении типа; закрытые Order не переписываются.
Retail без настроенной retail price остаётся на проверке, wholesale fallback запрещён.

| Область | Состояния |
|---|---|
| Draft | draft → needs_review ↔ ready → new (finalized); rejected |
| Fulfillment | new → assembling → assembled → shipped; cancelled до оплаты/отгрузки |
| Payment | unpaid → paid, независимо от сборки |
| Review | needs_review=true блокирует сборку; resolve снимает блокировку |
| Delivery | pending → ready (необязательно) → dispatched при отгрузке → delivered |
| Delivery method | unselected / pickup / manual / cdek / yandex |

План склада обязателен перед сборкой, полностью покрывает order items. Частичный
план не сохраняется. Один или два склада поддерживаются; deterministic split
не меняет исходные строки и суммы заказа. Остатки не резервируются внешне.

Суммы — integer minor units (копейки); qty — целое положительное. После finalize
операционные действия не меняют товары/суммы. Повторный и параллельный finalize
возвращают существующий заказ. Прямой internal POST /orders требует Idempotency-Key;
тот же ключ с другим payload даёт 409.

POST /orders/{id}/actions: internal token + X-Manager-Telegram-ID активного manager,
JSON action, expected_revision, idempotency_key; optional note/delivery fields.
Row lock сериализует изменения, stale version даёт 409. Повтор точного действия
с тем же ключом и actor возвращает текущее состояние без второго эффекта.
GET /orders/{id}/events — protected audit; GET /orders/{id} содержит новые поля,
UTC timestamps и actor IDs. OpenAPI: docs/openapi.json.

OrderEvent — отдельная append-only история на уровне application API. Приложение
не предоставляет API удаления/редактирования событий; DB admin остаётся доверенным.
Payment note хранится в заказе, не попадает в operational logs. Partial/refund
позже расширяют payment enum и отдельные действия, не fulfillment или technical status.

Миграция j93d5087bc10 additive: поля заказа/индексы/idempotency, order_events,
client_conversations, client_updates, расширение source=telegram. Старые заказы
получают new/unpaid; ранее rejected получают cancelled. Историческую фактическую
оплату/отгрузку нельзя угадать: она отмечается менеджером по проверенным данным.

## Restart guarantees

A process failure before finalize commit rolls back Order and its link; retry
creates one order. Failure after commit reuses finalized_order_id. Operational
idempotency and revision/audit are persisted together; two managers based on the
same revision cannot both mutate. A lost Telegram reply does not roll back a
committed business action. Refresh/own-status lookup is the recovery path.
External uncertain operations require reconciliation; no automatic replayed write.
