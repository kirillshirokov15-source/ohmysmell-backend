"""Internal setup API. Never mounted in the public catalog namespace."""
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.api.auth import require_internal_api_token
from app.database.session import async_session
from app.models.manager import Manager
from app.models.supply import Supplier, ProductSupply, SupplierOffer
from app.services.supply_service import ProcurementService, SupplyError
from app.services.fx import FxError

router = APIRouter(prefix="/internal/supply", tags=["Internal supply"], dependencies=[Depends(require_internal_api_token)])


async def manager_access(x_manager_telegram_id: int = Header()):
    async with async_session() as session:
        if not await session.scalar(select(Manager.id).where(Manager.telegram_id == x_manager_telegram_id, Manager.is_active.is_(True))):
            raise HTTPException(403, detail="Нет доступа менеджера")
    return x_manager_telegram_id


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SupplierInput(StrictBody):
    name: str = Field(min_length=1, max_length=255)
    supplier_type: Literal["partner_x", "external_wholesaler"]
    contact: str | None = Field(default=None, max_length=500)
    email: str | None = Field(default=None, max_length=320)
    external_reference: str | None = Field(default=None, max_length=255)


class SourceInput(StrictBody):
    source_type: Literal["own", "partner_x", "external"]
    supplier_id: int | None = Field(default=None, strict=True, gt=0)
    base_cost_minor: int | None = Field(default=None, strict=True, ge=0, le=9_223_372_036_854_775_807)


class OfferInput(StrictBody):
    product_id: str = Field(min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$")
    supplier_id: int = Field(strict=True, gt=0)
    supplier_sku: str | None = Field(default=None, max_length=255)
    purchase_price_minor: int = Field(strict=True, ge=0, le=9_223_372_036_854_775_807)
    currency_code: Literal["RUB", "USD", "EUR", "CNY"]
    availability: Literal["on_request", "confirmed", "unavailable"] = "on_request"
    availability_qty: int | None = Field(default=None, strict=True, ge=0)


@router.post("/suppliers", dependencies=[Depends(manager_access)])
async def supplier_create(payload: SupplierInput):
    async with async_session() as session, session.begin():
        supplier = Supplier(**payload.model_dump())
        session.add(supplier)
        await session.flush()
        return {"id": supplier.id}


@router.get("/suppliers", dependencies=[Depends(manager_access)])
async def suppliers_list(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
    async with async_session() as session:
        rows = list((await session.scalars(select(Supplier).order_by(Supplier.id).offset(offset).limit(limit))).all())
        return {"suppliers": [{"id": s.id, "name": s.name, "supplier_type": s.supplier_type, "status": s.status} for s in rows]}


@router.get("/offers", dependencies=[Depends(manager_access)])
async def offers_list(product_id: str = Query(min_length=1, max_length=255)):
    async with async_session() as session:
        rows = list((await session.scalars(select(SupplierOffer).where(SupplierOffer.product_id == product_id).order_by(SupplierOffer.id).limit(100))).all())
        return {"offers": [{"id": o.id, "supplier_id": o.supplier_id, "purchase_price_minor": o.purchase_price_minor,
            "currency_code": o.currency_code, "availability": o.availability, "active": o.active} for o in rows]}


@router.put("/products/{product_id}", dependencies=[Depends(manager_access)])
async def source_set(product_id: str, payload: SourceInput):
    if len(product_id) > 255:
        raise HTTPException(422, detail="Некорректный ID товара")
    async with async_session() as session, session.begin():
        from app.services.checkout_service import transaction_lock
        await transaction_lock(session, "supply:" + product_id)
        if payload.source_type == "partner_x":
            supplier = await session.get(Supplier, payload.supplier_id) if payload.supplier_id else None
            if not supplier or supplier.supplier_type != "partner_x" or payload.base_cost_minor is None:
                raise HTTPException(422, detail="Укажите партнёра X и базовую стоимость RUB")
        elif payload.supplier_id is not None or payload.base_cost_minor is not None:
            raise HTTPException(422, detail="Поставщик выбирается в предложении; базовая стоимость относится только к X")
        current = await session.get(ProductSupply, product_id)
        if current and current.source_type != payload.source_type:
            raise HTTPException(409, detail="Источник уже назначен. OWN и X взаимоисключающие; изменение требует отдельной проверки")
        if not current:
            current = ProductSupply(product_id=product_id)
            session.add(current)
        for key, value in payload.model_dump().items():
            setattr(current, key, value)
        return {"product_id": product_id, "source_type": current.source_type}


@router.post("/offers", dependencies=[Depends(manager_access)])
async def offer_create(payload: OfferInput):
    async with async_session() as session, session.begin():
        source = await session.get(ProductSupply, payload.product_id)
        supplier = await session.get(Supplier, payload.supplier_id)
        if not source or source.source_type != "external" or not supplier or supplier.supplier_type != "external_wholesaler":
            raise HTTPException(422, detail="Предложение требует внешний товар и внешнего поставщика")
        offer = SupplierOffer(**payload.model_dump())
        session.add(offer)
        await session.flush()
        return {"id": offer.id}


class ProcurementAction(StrictBody):
    action: Literal["select", "fx", "requested", "confirmed", "received", "cancelled", "unavailable"]
    expected_revision: int = Field(strict=True, ge=0)
    offer_id: int | None = Field(default=None, strict=True, gt=0)
    manual_rate: str | None = Field(default=None, max_length=30)


@router.put("/offers/{identifier}", dependencies=[Depends(manager_access)])
async def offer_update(identifier: int, payload: OfferInput):
    async with async_session() as session, session.begin():
        from app.services.buying import lock
        await lock(session)
        offer = await session.get(SupplierOffer, identifier, with_for_update=True)
        if not offer:
            raise HTTPException(404, detail="Предложение не найдено")
        if offer.product_id != payload.product_id or offer.supplier_id != payload.supplier_id:
            raise HTTPException(409, detail="Товар и поставщик предложения не меняются; создайте новое предложение")
        if offer.currency_code != payload.currency_code:
            raise HTTPException(409, detail="Для другой валюты создайте новое предложение; ручной курс существующей закупки привязан к её валюте")
        if offer.purchase_price_minor != payload.purchase_price_minor:
            from app.models.buying import BuyingPriceHistory
            session.add(BuyingPriceHistory(offer_id=offer.id, old_price_minor=offer.purchase_price_minor,
                new_price_minor=payload.purchase_price_minor, currency=offer.currency_code))
        offer.estimated_purchase_cost_rub_minor = None
        for key, value in payload.model_dump().items():
            setattr(offer, key, value)
        return {"id": offer.id}


@router.post("/offers/{identifier}/estimate", dependencies=[Depends(manager_access)])
async def offer_estimate(identifier: int):
    try:
        offer = await ProcurementService().refresh_estimate(identifier)
        return {"id": offer.id, "estimated_purchase_cost_rub_minor": offer.estimated_purchase_cost_rub_minor,
            "fx_rate_to_rub": str(offer.current_fx_rate_to_rub), "fx_source": offer.fx_source, "fx_rate_date": offer.fx_rate_date}
    except (SupplyError, FxError) as error:
        raise HTTPException(409, detail=str(error)) from error


@router.post("/procurements/{identifier}/actions")
async def procurement_action(identifier: int, payload: ProcurementAction, actor: int = Depends(manager_access)):
    try:
        r = await ProcurementService().act(identifier, actor, payload.action, payload.expected_revision,
            offer_id=payload.offer_id, manual_rate=payload.manual_rate)
        return {"id": r.id, "status": r.status, "revision": r.revision}
    except (SupplyError, FxError) as error:
        raise HTTPException(409, detail=str(error)) from error
