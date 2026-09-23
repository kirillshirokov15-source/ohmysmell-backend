# Manager Telegram workspace

Запуск: `python -m app.bot.runtime`, `BOT_ROLE=manager`. FastAPI не запускает polling.
Работает только с активными записями managers и только в личном чате.

Обеспечение: меню **«Требуют закупки»**, `/procurement [ORDER_ID]`, ручной курс
`/fx PROCUREMENT_ID REVISION 90.50`. Выберите поставщика → отметьте запрос отправленным
→ подтвердите закупку (фиксируется себестоимость) → отметьте получение.
Письма поставщикам бот не отправляет. Order card показывает Наш склад / X-склад /
Внешние поставщики, X 50/50 и fixed external cost/margin. Подробно:
[SUPPLY_AND_PROCUREMENT.md](SUPPLY_AND_PROCUREMENT.md).

Ежедневно:
1. `/start` открывает меню: новые заказы, заявки на проверку, готовые заявки,
   в сборке, собранные, отгруженные, оплаченные/неоплаченные и отменённые.
2. В заявке выбрать контрагента и сопоставить товары. Email всегда оптовый,
   Website всегда розничный: подтверждать/переключать тип для этих каналов не нужно.
   Для остальных каналов тип определяется профилем или проверкой менеджера.
   `/match НОМЕР_ЗАЯВКИ НОМЕР_ПОЗИЦИИ название` находит варианты.
   Конфликт типа профиля с каналом показан в карточке; профиль автоматически не меняется.
   Найденные email-позиции сразу имеют оптовую цену; неоднозначные — без цены до выбора.
3. Подтвердить готовую заявку: создаётся один локальный Order. «Открыть заказ»
   показывает товары, контакты, источник, цены в рублях, итог и склады.
4. «Распределить по складам» читает актуальные остатки и сохраняет полный план.
   Это не резерв и не отгрузка в МойСклад. Недостаточный остаток блокирует план.
5. «Начать сборку» → «Заказ собран» → выбрать способ доставки → «Отгружен».
6. После подтверждения фактического поступления денег нажать «Оплачен».
   `/payment НОМЕР примечание` делает то же с примечанием/референсом.
7. «Проблема / требуется проверка» блокирует продвижение сборки до
   «Проверка завершена». Неоплаченный, неотгруженный заказ можно отменить с подтверждением.

Поиск: `/order НОМЕР`, `/draft НОМЕР`; `/drafts OFFSET` — активные заявки.
Списки имеют кнопки страниц. `/items НОМЕР` показывает все позиции и распределение
большого заказа без обрезания. `/tracking НОМЕР reference` сохраняет номер доставки.
«Доставлен» доступно после отгрузки. Выбор СДЭК/Яндекс сохраняет предпочтение;
никаких внешних заявок или платежей не создаёт.

После ошибки или старой кнопки нажать «Обновить». Версия карточки проверяется в
транзакции; дубли не повторяют эффект. Потеря Telegram edit не отменяет сохранённое
действие: отправляется новая карточка. При недоступности БД — русская ошибка без traceback.

Информация об оплате: paid_at (UTC), paid_by_manager_id (PK managers), payment_note.
История OrderEvent хранит actor, action, before/after, timestamp и idempotency key.
HTTP actor header доверен только internal API caller; internal token никогда не
передаётся клиентам. Кнопки клиента в этом dispatcher отсутствуют.

## Recovery and process readiness

Explicit command: `python -m app.workers.manager_bot` (legacy app.bot.runtime with
BOT_ROLE=manager remains supported). SIGTERM, ownership loss and polling conflict
close the worker. API does not launch it. /health is liveness; /ready confirms
polling startup and DB ownership. After redeploy use the existing card: revision
and idempotency key are in PostgreSQL, not process memory. Two managers paying
concurrently cannot create two audit transitions. See INCIDENT_RUNBOOK.md.
# Buying sprint: shared manager group and quantities

Set `MANAGER_TELEGRAM_CHAT_ID` to the single allowed group/supergroup ID and
`MANAGER_TELEGRAM_USER_IDS` to comma-separated employee IDs. Each employee must
also be an active `managers` row. With a group configured, private-chat actions
are disabled and notifications target only the group. Without configuration,
existing private testing mode remains. No topics are used. Anonymous admin posts,
wrong chats, unknown actors and ordinary group conversation are ignored.
Keep BotFather privacy mode enabled; commands and inline callbacks are sufficient.

Use `/start`, `/orders`, `/drafts`, `/order ID`, `/draft ID`, existing procurement
commands and inline buttons. Group setup requires a real group ID; synthetic tests
do not constitute a live Telegram group acceptance.

Email quantities now carry `confirmed`, `probable`, or `unknown`. Cards warn on the
latter two. Buttons 1..5 confirm/replace quantity; manual entry is
`/quantity DRAFT_ID ITEM_ID QUANTITY REVISION`. The revision is shown by the manual
button, preventing stale edits. Audit stores actual callback/from_user Telegram ID,
old/new value, old confidence and timestamp. Finalization rejects every unconfirmed
quantity. Existing product/counterparty/order workflow remains.

Buying creation, fake send, supplier reply and received events are stored durably
and delivered by the manager worker's existing notification loop when a group is
configured. Delivery is at-least-once: a crash after Telegram accepts a message but
before DB commit can repeat the notification, never the purchase transition.
An event number permits recognition of duplicates. Events contain no customer data.
