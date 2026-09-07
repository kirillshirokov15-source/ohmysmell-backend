from fastapi import APIRouter, Header, HTTPException, Query
from app.config.settings import settings
from app.schemas.checkout import CheckoutCreate, CheckoutResponse, CatalogResponse, ApiError
from app.services.checkout_service import CheckoutService, CheckoutConflict
from app.services.product_service import ProductService
from app.services.stock_allocation import StockAllocationError

router = APIRouter(prefix="/api/v1", tags=["Website v1"], responses={
    status: {"model": ApiError} for status in (409, 413, 422, 503)
})


@router.get("/catalog", response_model=CatalogResponse)
async def catalog(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    products = await ProductService().get_catalog_async()
    return {"offset": offset, "limit": limit, "total": len(products), "products": [
        {"id": p["id"], "name": p["name"], "article": p.get("article"),
         "description": p.get("description"), "price_minor": p["price"],
         "available": p["total_available"] > 0, "requires_review": p["price"] is None}
        for p in products[offset:offset + limit]
    ]}


@router.post("/checkout", response_model=CheckoutResponse, status_code=202)
async def checkout(payload: CheckoutCreate, idempotency_key: str = Header(
    alias="Idempotency-Key", min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"
)):
    if not settings.public_checkout_enabled:
        raise HTTPException(503, detail={"code": "checkout_disabled", "message": "Приём заявок временно выключен"})
    try:
        return await CheckoutService().submit(payload, idempotency_key)
    except CheckoutConflict as error:
        raise HTTPException(409, detail={"code": "idempotency_conflict", "message": str(error)}) from error
    except StockAllocationError as error:
        raise HTTPException(409, detail={"code": "stock_unavailable", "message": str(error)}) from error
