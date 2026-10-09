# Client Telegram channel

Клиентский бот — дополнительный канал заявки, без интернет-магазина и платежей.
Отдельный dispatcher, отдельный `CLIENT_TELEGRAM_BOT_TOKEN`, `BOT_ROLE=client`.
Запуск `python -m app.bot.runtime`; менеджерский token использовать нельзя.
Текущий отдельный client token отсутствует: acceptance использует fake Telegram
transport с реальной staging DB. Код не зависит от Gmail/Tilda/retail price.

Диалог в личном чате:
- `/start`: команды и объяснение подтверждения цен менеджером.
- `/request`: товары по строкам `название или артикул; количество`.
- Отправить свой Telegram contact или ввести телефон/email.
- `/send`: атомарно сохранить inbound message, customer resolution, review draft
  и notification intent; получить номер заявки.
- `/status НОМЕР`: только собственная заявка, затем её заказ/сборка/оплата.
- `/manager`: повторно уведомить менеджера по последней своей заявке.
- `/cancel`: отменить ввод, не уже отправленную заявку.

Диалог и результаты update сохранены в PostgreSQL. Повторный update возвращает
прежний ответ. Ввод истекает через 24 часа бездействия. До 30 позиций, целое qty
1..10000. Цены не принимаются и не выводятся. Каталог не включён: свободный ввод
и manager matching покрывают канал без раскрытия оптовой цены.

Telegram numeric user ID приходит из доверенного update, не из сообщения.
Контакт считается подтверждённым только если contact.user_id совпадает с sender.
Подтверждённый телефон может связать Telegram с существующим профилем. Чужие
контакты отклоняются. Неподтверждённый контакт только направляет заявку в известный
профиль, не привязывает новую identity и не открывает данные профиля. Конфликт двух
существующих identities требует manager review; автоматического объединения нет.
Unknown остаётся unknown; профиль имеет приоритет над введёнными сведениями.

Клиент не может читать чужие заявки, менять тип клиента, передавать цены,
отмечать оплату или вызывать manager callback. Все callback отклоняются.

## Internal readiness additions

Explicit command: `python -m app.workers.client_bot`. It fixes the client role even
if BOT_ROLE is accidentally set to manager. Real aiogram Update/TelegramMethod
contract tests use fake BaseSession; PostgreSQL E2E recreates dispatcher/service
between messages and after engine disposal, including concurrent distinct /send.
Email contacts are stored in sender_email/contact_details and visible to manager;
no unverified customer identity is created from them. /ready is 503 until the
singleton owns polling; /health remains live during a deployment handover.
See EXTERNAL_INTEGRATIONS.md for exact token/live acceptance steps.
Client polling handles updates sequentially: dialogue messages remain ordered and
the polling offset advances after the handler completes its durable transaction.
Manager polling remains bounded concurrent; order row locks/revisions serialize effects.
# Клиент заказов Tilda

Для order desk: `CLIENT_TELEGRAM_ENABLED=true`, `CLIENT_ORDER_DESK_ENABLED=true`,
отдельный `CLIENT_TELEGRAM_BOT_TOKEN`; `ORDER_DESK_SEND_ENABLED=true` разрешает
исходящие Telegram sends. Все flags в примере выключены до staging acceptance.
Старый режим `/request` сохраняется при выключенном `CLIENT_ORDER_DESK_ENABLED`.
`/start OPAQUE_TOKEN` связывает только конкретную заявку; `/orders` — свои заявки,
`/status D` — публичный статус, `/message D текст` — сообщение менеджеру.
Личный Telegram Start обязателен; номер заказа сам по себе не даёт доступ.
Текст до 3000 UTF-16 units, без parse_mode, вложения не поддерживаются.
Подробности, TTL, безопасная выдача ссылки и Railway:
[TILDA_ORDER_FLOW.md](TILDA_ORDER_FLOW.md).
