"""Explicit, versioned manager commands for existing warehouse allocations."""
from aiogram import F
from aiogram.filters import Command, or_f
from app.services.order_source_service import OrderSourceService
from app.services.order_desk import DeskError
from app.schemas.order_sources import SourceAction


HELP = ("/sources ЗАКАЗ — позиции, назначения и ревизия\n"
        "/sourceadd ЗАКАЗ РЕВИЗИЯ ПОЗИЦИЯ КОЛИЧЕСТВО own СКЛАД\n"
        "/sourceadd ЗАКАЗ РЕВИЗИЯ ПОЗИЦИЯ КОЛИЧЕСТВО partner_x СКЛАД ПОСТАВЩИК БАЗА_КОП\n"
        "/sourceadd ЗАКАЗ РЕВИЗИЯ ПОЗИЦИЯ КОЛИЧЕСТВО external СКЛАД OFFER_ID [BUYING_ID]\n"
        "/sourceaction ЗАКАЗ РЕВИЗИЯ НАЗНАЧЕНИЕ receive|post_receipt|cancel\n"
        "Внешнюю закупку отправляйте и отмечайте полученной в Buying. "
        "После receive: /warehousedoc ЗАКАЗ РЕВИЗИЯ receipt НАЗНАЧЕНИЕ. "
        "Для реального МойСклад: /warehousedoc ЗАКАЗ РЕВИЗИЯ receipt|reserve|ship|release ID LIVE. "
        "Операция LIVE создаёт/изменяет реальный документ; повторы не отправляйте без сверки.")


def source_lines(view):
    from app.services.supply_service import LABELS
    from app.services.hatuna_post_pick import post_pick_enabled_for_order
    post_pick = post_pick_enabled_for_order(view["order_id"])
    lines = [f"Источники заказа №{view['order_id']} · ревизия {view['revision']}"]
    for item in view["items"]:
        label = ("учтено для отгрузки" if post_pick else "доступно к сборке")
        lines.append(f"Позиция #{item['id']} {item['name']}: {item['qty']} шт.; "
                     f"назначено {item['assigned']}; {label} {item['ready']}")
    states = {"awaiting_receipt": "ожидает получения", "received": "получено, требуется оприходование",
              "confirmed": "источник подтверждён", "cancelled": "отменено"}
    for a in view["allocations"]:
        source_state = (("назначено на склад Х; физическую сборку подтверждает человек"
                         if not a["ready"] else "источник подтверждён")
                        if post_pick and a["source_type"] == "partner_x"
                        else states[a["status"]])
        lines.append(f"Назначение #{a['id']} · позиция #{a['item_id']} · {LABELS[a['source_type']]} · "
            f"{a['qty']} шт. · склад {a['warehouse_id']} · {source_state} · план #{a.get('plan_id', '?')} {a['plan_status']} · "
            f"{'учтено для отгрузки' if a['ready'] else 'отгрузка заблокирована'}")
        if a["purchase_id"]:
            lines.append(f"Buying B-{a['purchase_id']:06d}: {a['purchase_status']}" +
                         (" · требуется оформить закупку" if a["requires_purchase"] else ""))
        if a["supplier_id"]:
            lines.append(f"Поставщик #{a['supplier_id']}")
        if a["receipt_simulated"]:
            lines.append("Получение/оприходование: симуляция")
    return lines


def register(dp, check_access):
    @dp.message(Command('hatuna'))
    async def hatuna_accounting_report(message):
        if not await check_access(message):
            return
        from app.services.hatuna_reporting import order_hatuna_report
        try:
            parts = (message.text or "").split()
            if len(parts) != 2:
                raise ValueError()
            order_id = int(parts[1])
            text = await order_hatuna_report(order_id, message.from_user.id, message.chat.id)
            await message.answer(text, parse_mode=None)
        except (ValueError, DeskError):
            await message.answer("Формат: /hatuna НОМЕР_ЗАКАЗА (только ответственный менеджер)", parse_mode=None)

    @dp.message(Command('hatunacheck'))
    async def hatuna_readonly_check(message):
        if not await check_access(message):
            return
        from app.services.hatuna_readonly_check import check_hatuna_order
        try:
            parts = (message.text or "").split()
            if len(parts) != 2:
                raise ValueError()
            report = await check_hatuna_order(int(parts[1]), message.from_user.id, message.chat.id)
            await message.answer(report, parse_mode=None)
        except (ValueError, DeskError):
            await message.answer("Формат: /hatunacheck НОМЕР_ЗАКАЗА (только ответственный менеджер)",
                                 parse_mode=None)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("hatuna_readonly_check_unavailable")
            await message.answer("Проверка склада Х недоступна. Ничего не проводите до сверки.",
                                 parse_mode=None)

    @dp.message(Command('warehousedoc'))
    async def warehouse_document(message):
        if not await check_access(message):
            return
        from app.services.warehouse_documents import WarehouseDocumentService
        from app.integrations.moysklad.documents import FakeDocumentAdapter
        from app.services.external_operation_service import ExternalOperationConflict
        from app.config.settings import settings
        try:
            parts=message.text.split()
            mode = settings.environment
            if mode == 'production':
                if len(parts) != 6 or parts[5] != 'LIVE':
                    raise DeskError('Для РЕАЛЬНОГО документа МойСклад явно укажите LIVE: /warehousedoc ЗАКАЗ РЕВИЗИЯ receipt|reserve|ship|release ID LIVE. Без LIVE запись запрещена.')
                if not settings.external_writes_enabled:
                    raise DeskError('Реальные операции МойСклад отключены.')
                service = WarehouseDocumentService()
            elif mode == 'staging':
                if len(parts) != 5:
                    raise DeskError('В staging используется только симуляция: /warehousedoc ЗАКАЗ РЕВИЗИЯ receipt|reserve|ship|release ID')
                service = WarehouseDocumentService(adapter=FakeDocumentAdapter())
            else:
                raise DeskError('Складские документы доступны только в настроенном окружении.')
            if parts[3] not in {'receipt','reserve','ship','release'}:
                raise DeskError('Неизвестный вид складского документа.')
            result=await service.act(int(parts[1]),message.from_user.id,message.chat.id,
                                     int(parts[2]),parts[3],int(parts[4]))
            label='МойСклад' if mode == 'production' else 'Симуляция'
            await message.answer(f"{label}: {result['action']} · документ {result['id']} · ревизия {result['revision']}",parse_mode=None)
        except (ValueError,DeskError,ExternalOperationConflict) as error:
            await message.answer(str(error),parse_mode=None)
        except Exception:
            # Unknown provider outcomes must be manually reconciled.
            # Never log order content, credentials or customer identities.
            import logging
            logging.getLogger(__name__).exception('warehouse_document_outcome_uncertain')
            await message.answer('МойСклад не подтвердил итог операции. НЕ повторяйте команду до проверки документа и журнала внешних операций.',parse_mode=None)

    @dp.message(or_f(Command("sources", "sourceadd", "sourceaction"), F.text == "Источники"))
    async def sources(message):
        if not await check_access(message):
            return
        try:
            parts = message.text.split()
            command, order_id = parts[0].split("@")[0], int(parts[1])
            service = OrderSourceService()
            if command == "/sources":
                view = await service.read(order_id, message.from_user.id, message.chat.id)
            else:
                revision = int(parts[2])
                if command == "/sourceadd":
                    item, qty, source, warehouse = int(parts[3]), int(parts[4]), parts[5], parts[6]
                    row = dict(item_id=item, qty=qty, source_type=source, warehouse_id=warehouse)
                    if source == "partner_x":
                        row.update(supplier_id=int(parts[7]), base_cost_minor=int(parts[8]))
                    elif source == "external":
                        row["offer_id"] = int(parts[7])
                        if len(parts)==9:
                            row['purchase_id']=int(parts[8])
                    expected = {"own": 7, "partner_x": 9, "external": 8}.get(source)
                    if source=='external' and 'purchase_id' in row:
                        expected=9
                    if len(parts) != expected:
                        raise ValueError()
                    payload = dict(action="assign", parts=[row])
                else:
                    if len(parts) != 5:
                        raise ValueError()
                    payload = dict(action=parts[4], allocation_id=int(parts[3]))
                request = SourceAction(**payload, expected_revision=revision,
                    idempotency_key=f"source:{message.chat.id}:{message.message_id}")
                await service.act(order_id, message.from_user.id, message.chat.id, request)
                view = await service.read(order_id, message.from_user.id, message.chat.id)
            chunk = ""
            for line in source_lines(view) + [HELP]:
                if len((chunk + line).encode("utf-16-le")) > 7500:
                    await message.answer(chunk, parse_mode=None)
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                await message.answer(chunk, parse_mode=None)
        except DeskError as error:
            await message.answer(str(error), parse_mode=None)
        except (ValueError, IndexError):
            await message.answer(HELP, parse_mode=None)
    return sources
