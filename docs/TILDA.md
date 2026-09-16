# Контракт для Tilda-разработчика: OhMySmell API v1

Tilda пока не подключена. Проверенный staging backend:
`https://ohmysmell-backend-staging-staging.up.railway.app`.
Финальный production origin задаётся отдельно. Токены CRM в браузер не передавать.
Схемы: `/openapi.json`, интерактивная спецификация: `/docs`.

## Каталог

`GET /api/v1/catalog?offset=0&limit=50`, максимум 100 товаров на страницу.

```json
{
  "products": [{
    "id": "product-uuid", "name": "Название товара", "article": "ARTICLE",
    "description": "Описание", "price_minor": null, "currency": "RUB",
    "available": true, "requires_review": true
  }],
  "offset": 0, "limit": 50, "total": 401
}
```

`total` меняется вместе с каталогом. Загружать страницы до `offset >= total`.
`price_minor=null` означает, что публичная розничная цена не настроена: показывать
«Цена по запросу», не ноль и не оптовую цену. Сейчас в МойСклад имеется только
«Цена продажи», которая является **оптовой**. Backend не применяет её к рознице.
`available` — снимок наличия, а не обещание резерва. Backend повторно проверяет
остатки при checkout. Архивные товары/склады исключены. Описания выводить через
`textContent`, не вставлять как непроверенный HTML. Защищённые MoySklad image URLs
не предназначены для браузера; фотографии пока управляются на Tilda.

## Checkout

`POST /api/v1/checkout`, JSON, обязательный header `Idempotency-Key`.
Ключ: случайный UUID, 16–128 символов `[A-Za-z0-9_-]`. Один ключ на логическую
заявку, сохранять при timeout/повторном клике/перезагрузке. Для изменённой корзины
создавать новый ключ. Backend хранит ключ без автоматического истечения.

```json
{
  "customer_name": "Имя покупателя",
  "email": "buyer@example.com",
  "phone": "+79990000000",
  "comment": "Свяжитесь со мной для согласования",
  "items": [{"product_id": "product-uuid", "qty": 2}]
}
```

Лимиты: 100 строк, quantity — integer 1–10000, имя 255, email 320, телефон 32,
комментарий 2000 символов; тело запроса до 256 KiB. Повторные строки одного товара
суммируются. Поля price, total, customer_type, customer_id, source запрещены.
Backend устанавливает `source=website`, сохраняет профиль существующего клиента,
выбирает допустимый прайс и рассчитывает деньги целыми копейками.

Успех — **202 Accepted**, это сохранённая заявка для менеджера, а не оплата,
подтверждённый заказ или резерв:

```json
{
  "request_id": "b25c399e-7eae-4ea3-a0b6-1a219964ed77",
  "status": "needs_review", "currency": "RUB", "total_minor": null,
  "message": "Заявка сохранена. Менеджер подтвердит цену и наличие."
}
```

Пока нет retail price, итог `null`. При будущем настроенном retail price backend
возвращает целочисленный итог, но всё равно сохраняет manager review. Указанный
email не доказывает владение оптовым аккаунтом: anonymous checkout не раскрывает
его оптовую цену и не переписывает профиль/контакты. Менеджер проверяет контактные
данные перед local finalize. Авторизация покупателя и онлайн-оплата не входят в v1.

## Ошибки и повторы

| HTTP | code | Поведение frontend |
|---|---|---|
| 409 | idempotency_conflict | Этот ключ уже использован с другим содержимым |
| 409 | stock_unavailable | Обновить каталог и корзину, показать сообщение |
| 422 | validation_error | Исправить поля из `detail.fields` |
| 413 | body_too_large | Уменьшить запрос |
| 503 | checkout_disabled | Приём заявок ещё не включён |
| 503 | service_unavailable | Повторить позже с тем же ключом |

Формат: `{"detail":{"code":"...","message":"..."}}`; у validation_error вместо
message возвращается список полей и типов ошибок без echo контактных данных.
Не разбирать русскую строку как машинный код. На сетевой timeout исход неизвестен:
повторить тот же payload с тем же ключом. Повтор возвращает сохранённый результат.

## Пример JavaScript

```javascript
const API = 'https://REPLACE-WITH-BACKEND.example';
const payload = {
  customer_name: form.name,
  email: form.email,
  phone: form.phone,
  comment: form.comment || null,
  items: cart.map(p => ({product_id: p.backendId, qty: p.quantity}))
};
// Сохраните key и payload вместе до завершения этой заявки.
const key = crypto.randomUUID();
async function submitSavedRequest() {
  const response = await fetch(`${API}/api/v1/checkout`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Idempotency-Key': key},
    body: JSON.stringify(payload)
  });
  const result = await response.json();
  if (response.status === 202) showSuccess(result.message);
  else showError(result.detail?.message || 'Проверьте данные или повторите позже');
}
```

Для staging в примере используйте
`https://ohmysmell-backend-staging-staging.up.railway.app`.
В staging включён `PUBLIC_CHECKOUT_ENABLED=true`; разрешены origins
`http://localhost:5500`, `http://127.0.0.1:5500` и тестовый
`https://ohmysmell-staging.example.invalid`. Последний — только placeholder для
preflight, сайт на нём не размещён. Перед подключением Tilda добавьте точный
HTTPS origin её тестовой страницы в staging `CORS_ORIGINS` и примените redeploy.
Cookies не используются (`allow_credentials=False`). Internal token не нужен
ни для каталога, ни для checkout и не должен попадать в Tilda-код.

В этом staging нет Telegram worker/poller: уведомления остаются в локальной
очереди. Проверить заявку можно через защищённые `/draft-orders` и
`/draft-orders/{id}` на стороне менеджера. Без розничного прайса local finalize
отклоняется: подключение сайта возможно в режиме «Цена по запросу / заявка»,
автоматическую оплату или подтверждённый розничный Order этот контракт не обещает.

## Перед подключением

1. Передать владельцу точные HTTPS origins опубликованного сайта и preview, если
   preview действительно нужен. CORS задаётся через `CORS_ORIGINS`, без `*`.
2. Сопоставить товары Tilda с backend `product_id`; не использовать индекс массива.
3. Не отправлять или доверять цене из корзины Tilda. Не включать автоматическую
   оплату по `total_minor=null` или до подтверждения цены менеджером.
4. На staging проверить пагинацию, «Цена по запросу», отсутствие товара, двойной
   submit, timeout/retry, 409, 422, disabled checkout и мобильный UX.
5. Для production требуется отдельное разрешение на включение
   `PUBLIC_CHECKOUT_ENABLED=true`, HTTPS, ограничение частоты/защиту от ботов
   на reverse proxy и проверенную production migration. Staging checkout уже включён.
6. Не использовать legacy `/orders` — теперь это защищённый внутренний endpoint.

Никаких изменений Tilda в рамках подготовки backend не выполнялось.


## Release candidate contract

Tilda remains unconnected. TODO: exact final Tilda HTTPS origin ? explicit CORS ?
browser checkout acceptance. Public catalog/request contract stays retail-only;
missing retail price is shown as price on request, with manager review and no payment.
Order operational states are internal. Never send X-Internal-API-Token or
X-Manager-Telegram-ID from Tilda. Client Telegram is an optional intake channel,
not a replacement storefront. Browser tests remain dependent on the final site.
