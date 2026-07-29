from fastapi import FastAPI
from app.schemas.order import OrderCreate
from app.database.connection import check_database_connection
from app.integrations.moysklad.client import MoySkladClient
from app.services.product_service import ProductService

app = FastAPI(
    title="OhMySmell API",
    version="0.1.0"
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

    return {
        "success": True,
        "received_order": order.model_dump()
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