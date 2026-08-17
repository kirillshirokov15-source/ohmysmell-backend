from fastapi import FastAPI, HTTPException
from app.schemas.order import OrderCreate
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
    set_order_counterparty,
)
from app.services.telegram_notification_service import notify_managers
from app.services.moysklad_order_service import (
    MoySkladOrderService,
    MoySkladOrderError,
)

app = FastAPI(
    title="OhMySmell API",
    version="0.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
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

    return {
        "database": "connected" if is_connected else "not connected"
    }


@app.post("/orders")
async def create_order(order: OrderCreate):
    service = OrderValidationService()

    try:
        validated_order = service.validate(order)

    except OrderValidationError as error:
        raise HTTPException(
            status_code=400,
            detail=str(error),
        )

    saved_order = await save_order(validated_order)

    await notify_managers(
        saved_order.id,
        validated_order,
    )

    return {
        "success": True,
        "order_id": saved_order.id,
        "status": "new",
        "order": validated_order,
    }

@app.get("/health/moysklad")
def health_moysklad():
    client = MoySkladClient()
    employee = client.get_current_user()

    return {
        "moysklad": "connected",
        "employee_id": employee.get("id"),
        "employee_name": employee.get("name"),
    }

@app.get("/products")
def get_products():
    service = ProductService()

    return {
        "products": service.get_catalog()
    }

@app.get("/stores")
def get_stores():
    client = MoySkladClient()

    return {
        "stores": client.get_stores()
    }

@app.get("/stocks")
def get_stocks():
    client = MoySkladClient()

    return {
        "stocks": client.get_stock_by_store()
    }

@app.get("/debug-products")
def debug_products():
    client = MoySkladClient()
    return client.get_products()[:1]

@app.get("/debug-stock")
def debug_stock():
    client = MoySkladClient()
    data = client.get_stock_by_store()
    return data.get("rows", [])[:1]

@app.get("/debug-product-images/{product_id}")
def debug_product_images(product_id: str):
    client = MoySkladClient()
    return {
        "images": client.get_product_images(product_id)
    }

@app.get("/debug-organizations")
def debug_organizations():
    client = MoySkladClient()

    return {
        "organizations": client.get_organizations()
    }

@app.get("/debug-counterparties")
def debug_counterparties(search: str):
    client = MoySkladClient()

    return {
        "counterparties": client.search_counterparties(search)
    }

@app.post("/orders/{order_id}/counterparty")
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

@app.post("/orders/{order_id}/moysklad")
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