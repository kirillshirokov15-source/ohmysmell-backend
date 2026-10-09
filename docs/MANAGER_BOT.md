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

Staging bootstrap diagnostic: with `APP_ENV=staging`, send `/chatid` (or
`/chatid@BOT_USERNAME`) from your personal Telegram account in the target group.
The reply contains only `chat_id=<numeric chat id>` and `user_id=<numeric sender id>`
on separate lines. No manager registration or group allowlist is needed for this
command. Anonymous admin posts are ignored because they do not identify the person.
Before IDs are known, use `MANAGER_TELEGRAM_MODE=auto` and leave both
`MANAGER_TELEGRAM_CHAT_ID` and `MANAGER_TELEGRAM_USER_IDS` empty; explicit `group`
mode still requires complete configuration at startup. Then configure the returned
IDs and restart the staging worker. All business commands and callbacks keep their
existing access checks. The diagnostic remains staging-only and is disabled in
production and development.

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

## Group configuration and Buying accounts (2026-09-25)

Set `MANAGER_TELEGRAM_MODE=group`, negative `MANAGER_TELEGRAM_CHAT_ID` and a
comma-separated `MANAGER_TELEGRAM_USER_IDS` list of positive human user IDs on the
staging manager service. Configure the same group metadata on the backend if its
Settings card should report group configuration. `auto` preserves private mode
when no group values exist. Explicit group mode with a missing chat, malformed IDs
or an empty actor allowlist fails startup. Local unit tests need no live values.

To obtain IDs without introducing another getUpdates poller: use Telegram Desktop
Export chat history (JSON) for the target group, then read its numeric chat ID and
`from_id` values for known managers. Alternatively, stop the staging poller, use
Telegram Bot API getUpdates locally with the existing token in a private client,
inspect `message.chat.id` and `message.from.id`, then restart the sole worker.
Never paste the bot-token URL in chat/logs or run a competing poller. Verify the
negative group ID against a controlled group command before enabling notification
routing. Add the bot to that group, preserve privacy mode, and allow each intended
manager both in the environment allowlist and the existing active Manager records.

Ordinary group text and anonymous-admin messages are ignored. Commands/buttons
require the allowed chat and actual from_user actor. Private and group notification
routing do not both fire. Buying purchase-created, simulated-email, supplier-reply
and received events use a durable notified marker. Received notifications include
purchase number, supplier and the persisted Buying username. Buying web roles are
separate from Telegram manager identities; they do not create Telegram access.

No live supplier send is enabled, so its real sent/error events are not emitted.
The notifier accepts event labels for future email_sent/procurement_error events.
Delivery remains at-least-once across a crash after Telegram accepts a message but
before DB commit; event IDs let operators recognize that narrow retry window.
Normal successful delivery and repeated received actions produce no duplicates.

## Supplier mailbox isolation — 2026-09-29

Supplier replies originate only from the separate supplier-email worker, never
from CUSTOMER Gmail. The manager notifier still delivers the same durable Buying
reply events and does not run another Gmail or Telegram intake loop. See
[GMAIL_MAILBOXES.md](GMAIL_MAILBOXES.md) for the two service/env configurations.
# Заказы сайта Tilda (staging MVP)

Полный контракт: [TILDA_ORDER_FLOW.md](TILDA_ORDER_FLOW.md).
`/site D` открывает заявку сайта; «Взять заказ» атомарно назначает ответственного.
`D` — номер заявки, `O` — номер подтверждённого Order (виден в карточке).
Только ответственный согласовывает сумму, отвечает и меняет статусы заказа.
`/reply D текст` — явный ответ клиенту; обычный текст/reply группы не пересылается.
`/sitereprice D` применяет исправленный оператором retail mapping к незакрытой заявке.
`/siteitems D` показывает полный список позиций и розничных цен.
Кнопки «Ожидает подтверждения», «Подтвердить товары и розничную сумму», «Ожидает оплаты»
дополняют существующие кнопки оплаты/сборки/доставки. Отгрузка полностью ручная.
`/outbox_retry ID` повторяет failed/blocked/uncertain сообщение после ручной проверки;
`/outbox_sent ID TELEGRAM_MESSAGE_ID` фиксирует подтверждённую доставку без повтора.
При uncertain Telegram мог уже доставить сообщение: сначала проверить адресата.
Новая очередь включается `ORDER_DESK_SEND_ENABLED=true` только для разрешённого smoke.
