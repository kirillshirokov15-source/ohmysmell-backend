# Обеспечение и закупки

Реализовано в staging, migration `k04e6198cd21`. Все суммы — integer minor units;
курс — Decimal, в JSON snapshot передаётся строкой. Внешние записи выключены.

## Единый каталог и источники

`ProductSupply.product_id` ссылается на существующую коммерческую идентичность товара
в каталоге МойСклад. Поставщик не создаёт второй товар. Для использования нового
внешнего SKU сначала нужна его привязка к товару этого каталога; отдельный импорт
каталога поставщика и автоматическое создание товаров сейчас не выполняются.

* **OWN / Наш склад** — значение по умолчанию без overlay. Остатки, резервы,
  склады и цены читает существующий адаптер МойСклад: `max(stock-reserve, 0)`.
* **PARTNER_X / X-склад** — один партнёр и базовая стоимость RUB на товар.
  Единственный `source_type` и PK product_id исключают одновременные OWN и X.
  API блокирует замену уже явно назначенного типа источника; миграция ownership
  требует отдельного согласованного решения. Физическое наличие X читается из
  существующего каталога/складов, новый склад не создаётся.
* **EXTERNAL / Внешние поставщики** — один логический bucket, несколько
  SupplierOffer разных поставщиков. Физических остатков/резервов/складов не создаёт.
  Доступность: on_request / confirmed / unavailable. Количество поставщика
  — его сообщение о наличии, а не собственный складской остаток.

Email/Gmail всегда wholesale; Website всегда retail, независимо от источника
обеспечения и профиля. Профиль при конфликте сохраняется. Розничная цена не
подменяется оптовой. Внешние товары доступны обоим каналам; retail без цены остаётся
на проверке. Ни закупочная цена, ни курс не определяют цену продажи автоматически.

## Фиксация источника и X

При создании Order в той же транзакции для каждой строки создаётся OrderItemSupply.
Для X также создаётся XSettlement, для EXTERNAL — один ProcurementRequest.
Существующие исторические заказы не переписываются автоматически.

X расчёт версии `line-50-50-v1` выполняется по **итогу строки с учётом количества**:

```
base = base_unit_cost * qty
sale = order_item.item_total
margin = sale - base
partner_margin = margin // 2
our_margin = margin - partner_margin
partner_due = base + partner_margin
```

Например, base=200000, sale=300001: X margin=50000, наша margin=50001,
к выплате X=250000 копеек. Нечётная копейка остаётся OhMySmell.
При sale < base сохраняется `requires_financial_review`, суммы к выплате/раздела
маржи отсутствуют, Order.needs_review=true. Обычный «Проверка завершена» не снимает
эту блокировку. Политика разрешения убыточных продаж не выдумывается: до решения
руководства такой заказ можно отменить и пересоздать с согласованной ценой.

PostgreSQL triggers запрещают UPDATE XSettlement и изменение зафиксированного
cost snapshot. Изменение предложения, курса или базовой цены не меняет историю.
Удаление synthetic заказа каскадно удаляет связанные operational rows; удаление
реальной финансовой истории не является менеджерским действием.

## Закупка и FX

`needed → requested → confirmed → received`; из открытых состояний также
`cancelled` / `unavailable`. Выбор поставщика — только вручную, до requested;
автоматического выбора самого дешёвого предложения нет. UNIQUE(order_item_id),
блокировки Order→ProcurementRequest и версия действия защищают от гонок/дубликатов.
Повтор того же действия того же менеджера возвращает уже сохранённый результат.
Другой устаревший callback отклоняется. Все изменения имеют SupplyEvent/manager_id.

«Запрос отправлен» лишь отмечает выполненное человеком действие; программа никому
не отправляет email. `SupplierCommunication` — интерфейс для будущего адаптера;
live send implementation отсутствует. Отмена заказа отменяет его открытые закупки.
Получение внешних товаров обязательно перед сборкой; собственные/X товары требуют
обычного складского плана. Для заказа только из external позиций фиктивный план
склада не нужен. Терминальная недоступная/отменённая закупка не переоткрывает
зафиксированную себестоимость: новый согласованный заказ создаётся отдельно.

RUB, USD, EUR, CNY используют два minor digits. `FxRateProvider` реализован
`CbrFxProvider` и `FakeFxProvider`; ручной курс задаётся менеджером.
[Официальный XML интерфейс ЦБ](https://www.cbr.ru/development/sxml/) вызывается только
GET: connect/read timeout 5/15 s, максимум два retry на безопасные ошибки чтения.
Используется дата, опубликованная ЦБ; последняя опубликованная котировка может
быть уже на следующий день. Курс Value делится на Nominal.

Оценка предложения обновляется отдельно; подтверждение не делает HTTP-запросов.
До confirmed менеджер обновляет курс ЦБ или вводит `/fx ID ВЕРСИЯ 90.50`.
RUB всегда имеет курс 1. Ручной курс сохраняет manager_id, timestamp и audit event.
При confirmed фиксируются цена за единицу в original currency, qty, rate/source/date,
manager ручного курса, время фиксации, RUB стоимость **всей строки**, цена продажи
и маржа. Перевод округляется один раз по сумме строки, ROUND_HALF_UP до копейки.
EXTERNAL: вся разница sale - fixed cost принадлежит OhMySmell, без X split.
Изменение предложения после подтверждения не меняет snapshot, даже если закупка
впоследствии отменена. Автоматических платежей или покупок нет.

## Настройка и работа менеджера

Все setup API требуют INTERNAL_API_TOKEN и активного `X-Manager-Telegram-Id`:

| Endpoint | Назначение |
|---|---|
| POST /internal/supply/suppliers | Supplier: name, supplier_type, optional contact/email/reference |
| PUT /internal/supply/products/{catalog_product_id} | source_type; для X supplier_id + base_cost_minor RUB |
| POST /internal/supply/offers | product_id, supplier_id, purchase_price_minor, currency_code, availability, optional qty/SKU |
| GET /internal/supply/suppliers; GET /internal/supply/offers?product_id=... | Найти существующие записи перед настройкой |
| PUT /internal/supply/offers/{id} | Изменить цену/наличие; product/supplier/currency неизменны, новая валюта требует нового offer |
| POST /internal/supply/offers/{id}/estimate | Обновить оценку по ЦБ |
| POST /internal/supply/procurements/{id}/actions | action, expected_revision, optional offer_id/manual_rate |

Сначала создать поставщика, назначить source_type existing catalog product, затем
создать offer. Для X offer не нужен: базовая RUB стоимость находится в ProductSupply.
Поставщика и предложение в setup создают однократно; повтор POST создаёт новую
запись конфигурации, поэтому при неопределённом результате нужно сверить выданный ID,
а не слепо повторять запрос. Сам Order/procurement защищён durable idempotency.

Telegram: меню «Требуют закупки», `/procurement` или `/procurement ORDER_ID`.
Карточка закупки показывает предложения, цену в original currency, оценку RUB и
контекстные кнопки выбора/запроса/подтверждения/получения/недоступности/отмены.
Карточка заказа показывает source, X split, fixed external cost и маржу.
Клиентский bot этот router не регистрирует. Публичный API сериализует whitelist:
product, retail price, availability_state; finance/supplier identity отсутствуют.

## Проверки и эксплуатационные границы

Synthetic staging E2E: OWN + X + external USD; ручной выбор второго поставщика;
конкурентные повторные действия; fixed cost после смены offer/FX; restart; DB trigger
immutability; Russian manager card. Отдельно проверяется external через email
ingestion/finalize и website checkout/finalize. HTTP CBR smoke выполнен read-only.

При rollout сначала `alembic upgrade head` в явно выбранном staging environment,
затем staging backend/manager/email deploy. Production не затрагивается.
Downgrade удаляет только семь новых таблиц и два snapshot trigger; допустим лишь
на disposable schema или после отдельного решения о сохранении данных закупок.
EXTERNAL_WRITES_ENABLED=false; Gmail selector/OAuth/cursor не меняются.

Осталось согласовать/внести реальные данные: какие product IDs принадлежат X,
партнёр X и базовые цены, список внешних поставщиков/предложений, допустимый возраст
курса для закупки и порядок индивидуального одобрения убыточной продажи X.
Импорт supplier catalog и автоматическая supplier communication — отдельные
интеграции за чистыми текущими границами, сейчас не активированы.
# Buying extension

Buying reuses `suppliers`, `product_supply`, and `supplier_offers`. Separate additive
tables store parser/name mappings, local canonical names, import previews/history,
shared cart, checkout keys, purchases/snapshots, replies and audit/group events.
Buying purchases have no customer or order foreign keys. Existing wholesale/retail
procurement and X settlement keep their existing workflow.

One checkout makes one purchase per supplier and snapshots original procurement
prices, FX date/source/rate and approximate RUB values. Later price lists or FX changes
never recalculate those snapshots. Cart always displays current price and flags changes
since add. Preview fingerprints and DB advisory locks prevent stale checkout and
duplicate side effects. Marking received records one audit event and invokes only
the fake receipt adapter in this slice. All suppliers belong to the same logical
warehouse “Внешние поставщики”.

See [Buying API](BUYING_API.md) for frontend contract and explicit live-write limitations.

## Buying roles and pickup audit (2026-09-25)

Buying now authenticates database accounts with manager/picker roles, not a shared
password. See [BUYING_ROLES.md](BUYING_ROLES.md) and the complete
[BUYING_API.md](BUYING_API.md). Receiving persists the first user ID, role, username
and time with one durable audit event. Replays preserve that actor. Existing old
received purchases retain null actor fields. Suppliers may have shared pickup
address, phone and notes; pickup responses exclude all procurement prices/currency.
All real email/MoySklad write guards remain disabled; fake hooks are unchanged.

## Procurement Gmail account binding — 2026-09-29

Migration o48ca532ab65 adds an internal supplier_mailbox_account field to purchases.
Only threads bound to the verified SUPPLIER address are eligible for polling.
Existing unbound references are quarantined; fake IDs stay excluded. Future live
sending must persist the verified account alongside Gmail message/thread IDs.
Customer token/scopes/runtime are never used for supplier sending or reply reads.
No outbound behavior or procurement-cost snapshot changes are enabled here.
