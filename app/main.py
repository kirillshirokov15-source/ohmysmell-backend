import logging
from contextlib import asynccontextmanager
from app.config.settings import settings


@asynccontextmanager
async def lifespan(app):
    settings.validate_runtime()
    from app.logging_utils import configure_application_logging
    configure_application_logging()
    yield
    from app.database.session import engine
    await engine.dispose()

from fastapi import Depends, FastAPI, HTTPException, Header
from app.api.auth import require_debug_access, require_internal_api_token
from app.schemas.order import OrderCreate
from app.schemas.order_read import OrderRead
from app.schemas.draft_order import (
    InboundEmailCreate,
    LinkCounterpartyRequest,
    ResolveProductRequest,
    SetCustomerTypeRequest,
)
from app.database.connection import check_database_connection
from app.integrations.moysklad.client import MoySkladClient
from app.services.product_service import ProductService
from fastapi.middleware.cors import CORSMiddleware
from app.services.order_validation_service import (
    OrderValidationService,
    OrderValidationError,
)
from app.repositories.order_repository import (
    create_order as save_order,
    get_order,
    list_orders,
    set_order_counterparty,
)
from app.services.telegram_notification_service import notify_managers
from app.services.moysklad_order_service import (
    MoySkladOrderService,
    MoySkladOrderError,
)

app = FastAPI(
    title="OhMySmell API",
    version="1.0.0",
    lifespan=lifespan,
)
from app.api.public import router as public_router
app.include_router(public_router)
from app.api.safety import install_error_handlers, RequestSafetyMiddleware
install_error_handlers(app)
app.add_middleware(RequestSafetyMiddleware)
from app.services.customer_resolution_service import (
    CustomerResolutionError,
    CustomerResolutionService,
)
from app.integrations.email.provider import EmailMessage
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.draft_order_service import DraftOrderError, DraftOrderService
from app.services.money import format_rubles

logger = logging.getLogger(__name__)


def serialize_order(order):
    return {
        "id": order.id,
        "customer_id": order.customer_id,
        "customer_name": order.customer_name,
        "customer_type": order.customer_type,
        "source": order.source,
        "status": order.status,
        **{key: getattr(order, key, None) for key in ("revision", "fulfillment_status", "payment_status", "needs_review", "status_changed_at", "status_changed_by_manager_id", "assembling_at", "assembled_at", "shipped_at", "paid_at", "paid_by_manager_id", "payment_note", "delivery_method", "delivery_status", "delivery_reference")},
        "phone": order.phone,
        "email": getattr(order, "customer_email", None),
        "telegram": order.telegram,
        "counterparty_id": order.counterparty_id,
        "counterparty_name": order.counterparty_name,
        "total": order.total,
        "total_minor": order.total,
        "total_major": format_rubles(order.total),
        "created_at": order.created_at,
        "items": [
            {
                "id": item.id,
                "product_id": item.product_id,
                "name": item.name,
                "article": item.article,
                "price": item.price,
                "price_minor": item.price,
                "price_major": format_rubles(item.price),
                "qty": item.qty,
                "item_total": item.item_total,
                "item_total_minor": item.item_total,
                "item_total_major": format_rubles(item.item_total),
            }
            for item in order.items
        ],
    }


def serialize_draft(draft):
    return {
        "id": draft.id,
        "revision": draft.revision,
        "inbound_message_id": draft.inbound_message_id,
        "customer_id": draft.customer_id,
        "customer_type": draft.customer_type,
        "source": draft.source,
        "status": draft.status,
        "sender_email": draft.sender_email,
        "contact_details": draft.contact_details,
        "customer_name": draft.customer_name,
        "subject": draft.subject,
        "counterparty_id": draft.counterparty_id,
        "counterparty_name": draft.counterparty_name,
        "counterparty_candidates": draft.counterparty_candidates,
        "total": draft.total,
        "total_minor": draft.total,
        "total_major": (
            format_rubles(draft.total) if draft.total is not None else None
        ),
        "review_notes": draft.review_notes,
        "finalized_order_id": draft.finalized_order_id,
        "created_at": draft.created_at,
        "items": [
            {
                "id": item.id,
                "raw_product_text": item.raw_product_text,
                "qty": item.qty,
                "match_status": item.match_status,
                "product_id": item.product_id,
                "product_name": item.product_name,
                "article": item.article,
                "price": item.price,
                "price_minor": item.price,
                "price_major": (
                    format_rubles(item.price) if item.price is not None else None
                ),
                "item_total": item.item_total,
                "item_total_minor": item.item_total,
                "item_total_major": (
                    format_rubles(item.item_total)
                    if item.item_total is not None else None
                ),
                "candidates": item.candidates,
            }
            for item in draft.items
        ],
    }

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "Idempotency-Key", "X-Internal-API-Token"],
)


@app.get("/")
def root():
    return {
        "status": "OK",
        "message": "OhMySmell Backend is running!"
    }


@app.get("/health/db")
async def health_db():
    is_connected = await check_database_connection()
    if not is_connected:
        raise HTTPException(503, detail="Database unavailable")

    return {
        "database": "connected" if is_connected else "not connected"
    }


@app.post("/orders", dependencies=[Depends(require_internal_api_token)])
async def create_order(order: OrderCreate, idempotency_key: str = Header(min_length=1, max_length=128)):
    import hashlib
    from app.repositories.order_repository import get_order_by_request_key
    from app.services.checkout_service import CheckoutConflict
    digest = hashlib.sha256(order.model_dump_json().encode()).hexdigest()
    existing = await get_order_by_request_key(idempotency_key)
    if existing:
        if existing.request_hash != digest:
            raise HTTPException(409, detail="Ключ запроса уже использован")
        return {"success": True, "order_id": existing.id, "status": existing.status, "order": serialize_order(existing)}
    try:
        customer = await CustomerResolutionService().resolve(order)
    except CustomerResolutionError as error:
        raise HTTPException(status_code=409, detail=str(error))

    order = order.model_copy(
        update={"customer_type": customer.customer_type}
    )
    service = OrderValidationService()

    try:
        validated_order = await service.validate_async(order)
        validated_order["customer_id"] = customer.customer_id
        validated_order["counterparty_id"] = (
            customer.moysklad_counterparty_id
        )

    except OrderValidationError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    validated_order.update(_request_key=idempotency_key, _request_hash=digest)
    try:
        saved_order = await save_order(validated_order)
    except CheckoutConflict as error:
        raise HTTPException(409, detail=str(error)) from error

    try:
        await notify_managers(
            saved_order.id,
            validated_order,
        )
    except Exception:
        logger.warning(
            "Order %s was saved, but Telegram notification failed",
            saved_order.id,
        )

    return {
        "success": True,
        "order_id": saved_order.id,
        "status": "new",
        "order": serialize_order(saved_order),
    }

@app.get("/health/moysklad", dependencies=[Depends(require_internal_api_token)])
def health_moysklad():
    client = MoySkladClient()
    employee = client.get_current_user()

    return {
        "moysklad": "connected",
        "employee_id": employee.get("id"),
        "employee_name": employee.get("name"),
    }

@app.get("/products", deprecated=True)
async def get_products():
    service = ProductService()

    return {
        "products": await service.get_catalog_async()
    }

@app.get("/stores", dependencies=[Depends(require_internal_api_token)])
def get_stores():
    client = MoySkladClient()

    return {
        "stores": client.get_stores()
    }

@app.get("/stocks", dependencies=[Depends(require_internal_api_token)])
def get_stocks():
    client = MoySkladClient()

    return {
        "stocks": client.get_stock_by_store()
    }

@app.get("/debug-products", dependencies=[Depends(require_debug_access)])
def debug_products():
    client = MoySkladClient()
    return client.get_products()[:1]

@app.get("/debug-stock", dependencies=[Depends(require_debug_access)])
def debug_stock():
    client = MoySkladClient()
    data = client.get_stock_by_store()
    return data.get("rows", [])[:1]

@app.get(
    "/debug-product-images/{product_id}",
    dependencies=[Depends(require_debug_access)],
)
def debug_product_images(product_id: str):
    client = MoySkladClient()
    return {
        "images": client.get_product_images(product_id)
    }

@app.get("/debug-organizations", dependencies=[Depends(require_debug_access)])
def debug_organizations():
    client = MoySkladClient()

    return {
        "organizations": client.get_organizations()
    }

@app.get("/debug-counterparties", dependencies=[Depends(require_debug_access)])
def debug_counterparties(search: str):
    client = MoySkladClient()

    return {
        "counterparties": client.search_counterparties(search)
    }

@app.post(
    "/orders/{order_id}/counterparty",
    dependencies=[Depends(require_internal_api_token)],
)
async def assign_counterparty(
    order_id: int,
    counterparty_id: str,
    counterparty_name: str,
):
    order = await set_order_counterparty(
        order_id=order_id,
        counterparty_id=counterparty_id,
        counterparty_name=counterparty_name,
    )

    if order is None:
        raise HTTPException(
            status_code=404,
            detail="Заказ не найден",
        )

    return {
        "success": True,
        "order_id": order.id,
        "counterparty_id": order.counterparty_id,
        "counterparty_name": order.counterparty_name,
    }

@app.post(
    "/orders/{order_id}/moysklad",
    dependencies=[Depends(require_internal_api_token)],
)
async def create_order_in_moysklad(order_id: int):
    service = MoySkladOrderService()

    try:
        result = await service.create_from_crm_order(
            order_id
        )

    except MoySkladOrderError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    return {
        "success": True,
        "crm_order_id": order_id,
        "moysklad_order_id": result.get("id"),
        "moysklad_order_name": result.get("name"),
    }


@app.get("/orders", dependencies=[Depends(require_internal_api_token)])
async def get_orders():
    return {"orders": [serialize_order(order) for order in await list_orders()]}


@app.get("/orders/{order_id}", response_model=OrderRead, dependencies=[Depends(require_internal_api_token)])
async def get_order_details(order_id: int):
    order = await get_order(order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="Заказ не найден")
    return serialize_order(order)


@app.post(
    "/internal/email/messages",
    dependencies=[Depends(require_internal_api_token)],
)
async def ingest_email_message(payload: InboundEmailCreate):
    message = EmailMessage(
        external_message_id=payload.external_message_id,
        sender_email=payload.sender_email,
        sender_name=payload.sender_name,
        subject=payload.subject,
        body_text=payload.body_text,
        received_at=payload.received_timestamp(),
    )
    try:
        draft = await DraftOrderService().ingest_email(message)
    except DraftOrderError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return serialize_draft(draft)


@app.get("/draft-orders", dependencies=[Depends(require_internal_api_token)])
async def get_draft_orders():
    drafts = await DraftOrderRepository().list()
    return {"draft_orders": [serialize_draft(draft) for draft in drafts]}


@app.get(
    "/draft-orders/{draft_id}",
    dependencies=[Depends(require_internal_api_token)],
)
async def get_draft_order(draft_id: int):
    draft = await DraftOrderRepository().get(draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail="Draft не найден")
    return serialize_draft(draft)


@app.post(
    "/draft-orders/{draft_id}/customer-type",
    dependencies=[Depends(require_internal_api_token)],
)
async def set_draft_customer_type(
    draft_id: int, payload: SetCustomerTypeRequest
):
    if payload.customer_type == "unknown":
        raise HTTPException(status_code=400, detail="Нужно выбрать wholesale или retail")
    try:
        draft = await DraftOrderService().set_customer_type(
            draft_id, payload.customer_type
        )
    except DraftOrderError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return serialize_draft(draft)


@app.post(
    "/draft-orders/{draft_id}/items/{item_id}/match",
    dependencies=[Depends(require_internal_api_token)],
)
async def resolve_draft_product(
    draft_id: int, item_id: int, payload: ResolveProductRequest
):
    try:
        draft = await DraftOrderService().resolve_product(
            draft_id, item_id, payload.product_id
        )
    except DraftOrderError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return serialize_draft(draft)


@app.post(
    "/draft-orders/{draft_id}/counterparty",
    dependencies=[Depends(require_internal_api_token)],
)
async def link_draft_counterparty(
    draft_id: int, payload: LinkCounterpartyRequest
):
    try:
        draft = await DraftOrderService().link_counterparty(
            draft_id, payload.counterparty_id, payload.counterparty_name
        )
    except DraftOrderError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return serialize_draft(draft)


@app.post(
    "/draft-orders/{draft_id}/reject",
    dependencies=[Depends(require_internal_api_token)],
)
async def reject_draft_order(draft_id: int):
    try:
        draft = await DraftOrderService().reject(draft_id)
    except DraftOrderError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return serialize_draft(draft)


@app.post(
    "/draft-orders/{draft_id}/finalize",
    dependencies=[Depends(require_internal_api_token)],
)
async def finalize_draft_order(draft_id: int):
    try:
        order = await DraftOrderService().finalize(draft_id)
    except DraftOrderError as error:
        raise HTTPException(status_code=400, detail=str(error))
    return {"success": True, "order": serialize_order(order)}


@app.post("/orders/{order_id}/allocations", dependencies=[Depends(require_internal_api_token)])
async def plan_order_allocations(order_id: int):
    from app.services.fulfillment_service import FulfillmentService
    from app.services.stock_allocation import StockAllocationError
    try:
        shipments = await FulfillmentService().plan(order_id)
    except StockAllocationError as error:
        raise HTTPException(409, detail=str(error)) from error
    return {"order_id": order_id, "inventory_reserved": False, "shipments": [
        {"id": s.id, "warehouse_id": s.warehouse_id, "status": s.status,
         "allocations": [{"order_item_id": a.order_item_id, "qty": a.qty} for a in s.allocations]}
        for s in shipments]}


@app.post("/draft-orders/{draft_id}/items/{item_id}/candidates", dependencies=[Depends(require_internal_api_token)])
async def propose_draft_candidates(draft_id: int, item_id: int, query: str):
    return serialize_draft(await DraftOrderService().search_item(draft_id, item_id, query))


from app.services.order_operations import OrderAction, OrderOperations, OperationError, ManagerDenied
from fastapi import Header


@app.post("/orders/{order_id}/actions", response_model=OrderRead, dependencies=[Depends(require_internal_api_token)])
async def order_action(order_id: int, payload: OrderAction, x_manager_telegram_id: int = Header()):
    try:
        return serialize_order(await OrderOperations().act(order_id, x_manager_telegram_id, payload))
    except ManagerDenied as error:
        raise HTTPException(403, detail=str(error)) from error
    except OperationError as error:
        raise HTTPException(409, detail=str(error)) from error


@app.get("/orders/{order_id}/events", dependencies=[Depends(require_internal_api_token)])
async def order_events(order_id: int):
    return {"events": [{"id": e.id, "manager_id": e.manager_id, "action": e.action,
        "before": e.before, "after": e.after, "created_at": e.created_at}
        for e in await OrderOperations().events(order_id)]}
