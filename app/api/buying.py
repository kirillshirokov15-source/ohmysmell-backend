"""Internal SPA API. All workspace routes require a revocable shared session."""
import hmac
import hashlib
import secrets
import time
from collections import deque
from datetime import timedelta
from typing import Literal
from pathlib import PurePath
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, delete, func, case, or_
from starlette.concurrency import run_in_threadpool
from app.config.settings import settings
from app.database.session import async_session
from app.models.buying import BuyingSession, BuyingSupplier, BuyingOffer, BuyingProduct, BuyingImport, BuyingCart, BuyingPriceHistory, BuyingPurchase, BuyingReply
from app.models.supply import Supplier, SupplierOffer, ProductSupply
from app.services import buying as service
from app.services.buying_excel import ParserConfig, ColumnPriceListParser, MAX_UPLOAD, normalize
from app.services.fx import CbrFxProvider, FxError
from app.schemas.buying import CatalogRead, OffersRead, CartRead, CheckoutPreviewRead, PurchaseRead, PurchaseDetailRead, PurchasesRead

auth_router = APIRouter(prefix="/buying/auth", tags=["Buying auth"])
bearer = HTTPBearer(auto_error=False)
attempts = deque(maxlen=100)


def configured():
    if len(settings.buying_shared_password) < 12 or len(settings.buying_session_secret) < 32:
        raise HTTPException(503, "Buying authentication is not configured")


def digest(token):
    key = (settings.buying_session_secret + settings.buying_shared_password).encode()
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


async def session_access(response: Response, credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    configured()
    response.headers["Cache-Control"] = "no-store"
    if not credentials or len(credentials.credentials) > 200:
        raise HTTPException(401, "Buying session required")
    key = digest(credentials.credentials)
    async with async_session() as session:
        record = await session.get(BuyingSession, key)
        if not record or record.expires_at.timestamp() <= service.now().timestamp():
            raise HTTPException(401, "Buying session expired or revoked")
    return key


router = APIRouter(prefix="/buying", tags=["Buying"], dependencies=[Depends(session_access)])


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Login(Body):
    # Password whitespace is meaningful.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    password: str = Field(min_length=1, max_length=1024)


@auth_router.post("/login")
async def login(payload: Login, response: Response) -> dict:
    configured()
    current = time.monotonic()
    while attempts and attempts[0] < current - 60:
        attempts.popleft()
    if len(attempts) >= 30:
        raise HTTPException(429, "Too many login attempts", headers={"Retry-After": "60"})
    attempts.append(current)
    if not hmac.compare_digest(payload.password.encode(), settings.buying_shared_password.encode()):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(32)
    async with async_session() as session, session.begin():
        await session.execute(delete(BuyingSession).where(BuyingSession.expires_at <= service.now()))
        session.add(BuyingSession(digest=digest(token), expires_at=service.now() + timedelta(hours=8)))
    response.headers["Cache-Control"] = "no-store"
    return {"access_token": token, "token_type": "bearer", "expires_in": 28800}


@auth_router.post("/logout")
async def logout(key: str = Depends(session_access)) -> dict:
    async with async_session() as session, session.begin():
        await session.execute(delete(BuyingSession).where(BuyingSession.digest == key))
    return {"revoked": True}


class SupplierInput(Body):
    name: str = Field(min_length=1, max_length=255)
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$")
    currency: Literal["RUB", "USD"]
    parser: ParserConfig


@router.post("/suppliers")
async def create_supplier(payload: SupplierInput) -> dict:
    try:
        ColumnPriceListParser(payload.parser)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    async with async_session() as session, session.begin():
        s = Supplier(supplier_type="external_wholesaler", name=payload.name, email=payload.email)
        session.add(s)
        await session.flush()
        session.add(BuyingSupplier(supplier_id=s.id, currency=payload.currency, parser=payload.parser.model_dump()))
        return {"id": s.id}


async def suppliers_data(session, identifier=None, offset=0, limit=50):
    count = select(func.count(SupplierOffer.id)).where(SupplierOffer.supplier_id == Supplier.id, SupplierOffer.active.is_(True)).correlate(Supplier).scalar_subquery()
    latest = select(func.max(BuyingImport.imported_at)).where(BuyingImport.supplier_id == Supplier.id).correlate(Supplier).scalar_subquery()
    query = select(Supplier, BuyingSupplier, count, latest).join(BuyingSupplier)
    if identifier is not None:
        query = query.where(Supplier.id == identifier)
    rows = (await session.execute(query.order_by(Supplier.id).offset(offset).limit(limit))).all()
    return [dict(id=s.id, name=s.name, email=s.email, currency=b.currency, status=s.status,
        latest_price_list_upload=latest, active_offer_count=count, created_at=s.created_at, updated_at=s.updated_at,
        parser=b.parser) for s,b,count,latest in rows]


@router.get("/suppliers")
async def suppliers(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)) -> dict:
    async with async_session() as session:
        return {"suppliers": await suppliers_data(session, offset=offset, limit=limit), "offset": offset, "limit": limit}


@router.get("/suppliers/{supplier_id}")
async def supplier_detail(supplier_id: int) -> dict:
    async with async_session() as session:
        result = await suppliers_data(session, supplier_id)
        if not result:
            service.fail("Supplier not found", 404)
        return result[0]


@router.post("/suppliers/{supplier_id}/price-lists/preview")
async def import_preview(supplier_id: int, request: Request, filename: str = Query(min_length=6, max_length=255)) -> dict:
    if "/" in filename or "\\" in filename or not filename.lower().endswith(".xlsx") or any(ord(c) < 32 for c in filename):
        raise HTTPException(422, "A plain .xlsx filename is required")
    data = await request.body()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Upload exceeds 2 MiB")
    async with async_session() as session, session.begin():
        _, config = await service.supplier_get(session, supplier_id)
        try:
            rows = await run_in_threadpool(ColumnPriceListParser(ParserConfig(**config.parser)).parse, data)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        record = await service.preview_import(session, supplier_id, filename, rows)
        return {"import_id": record.id, "counts": record.counts, "rows": record.rows, "currency": record.currency, "parser_version": record.parser_version}


class ImportConfirm(Body):
    import_id: str = Field(min_length=36, max_length=36)
    mappings: dict[str, str] = Field(default_factory=dict, max_length=5000)


@router.post("/suppliers/{supplier_id}/price-lists/import")
async def import_confirm(supplier_id: int, payload: ImportConfirm) -> dict:
    async with async_session() as session, session.begin():
        r = await service.confirm_import(session, supplier_id, payload.import_id, payload.mappings)
        return {"import_id": r.id, "imported_at": r.imported_at, "counts": r.counts}


class MappingInput(Body):
    product_id: str = Field(min_length=1, max_length=255)
    name: str = Field(min_length=1, max_length=500)


@router.post("/suppliers/{supplier_id}/configure")
async def configure_supplier(supplier_id: int, payload: SupplierInput) -> dict:
    """Adopt an existing external supplier without duplicating its identity."""
    try:
        ColumnPriceListParser(payload.parser)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    async with async_session() as session, session.begin():
        await service.lock(session)
        supplier = await session.get(Supplier, supplier_id)
        if not supplier or supplier.supplier_type != 'external_wholesaler':
            service.fail('External supplier not found',404)
        if await session.get(BuyingSupplier,supplier_id):
            service.fail('Supplier already configured')
        if await session.scalar(select(SupplierOffer.id).where(SupplierOffer.supplier_id==supplier_id, SupplierOffer.currency_code!=payload.currency).limit(1)):
            service.fail('Existing offers have incompatible currency')
        supplier.name, supplier.email = payload.name, payload.email
        session.add(BuyingSupplier(supplier_id=supplier_id,currency=payload.currency,parser=payload.parser.model_dump()))
        return {'id':supplier_id}


class OfferMapping(Body):
    name: str = Field(min_length=1,max_length=500)


@router.post("/offers/{offer_id}/mapping")
async def map_existing_offer(offer_id: int,payload: OfferMapping) -> dict:
    if len(normalize(payload.name)) > 495:
        raise HTTPException(422, 'Normalized supplier name exceeds mapping limit')
    async with async_session() as session,session.begin():
        await service.lock(session)
        offer=await session.get(SupplierOffer,offer_id)
        if not offer:
            service.fail('Offer not found',404)
        _,config=await service.supplier_get(session,offer.supplier_id)
        if offer.currency_code!=config.currency or any(ord(c)<32 for c in payload.name):
            service.fail('Invalid offer currency/name')
        key='sku:'+offer.supplier_sku if offer.supplier_sku else 'name:'+normalize(payload.name)
        existing=await session.scalar(select(BuyingOffer).where(BuyingOffer.supplier_id==offer.supplier_id,BuyingOffer.mapping_key==key))
        if existing and existing.offer_id!=offer_id:
            service.fail('Supplier mapping already exists')
        if not await session.get(BuyingOffer,offer_id):
            session.add(BuyingOffer(offer_id=offer_id,supplier_id=offer.supplier_id,mapping_key=key,name=payload.name))
        if not await session.get(BuyingProduct,offer.product_id):
            session.add(BuyingProduct(product_id=offer.product_id,name=payload.name,normalized_name=normalize(payload.name)))
        return {'offer_id':offer_id,'product_id':offer.product_id}


@router.post("/products/mappings")
async def map_product(payload: MappingInput) -> dict:
    """Register an existing supply identity for exact-name matching before import."""
    if len(normalize(payload.name)) > 500:
        raise HTTPException(422, 'Normalized product name exceeds mapping limit')
    async with async_session() as session, session.begin():
        await service.lock(session)
        if not await session.get(ProductSupply, payload.product_id):
            service.fail("Existing supply product required", 404)
        p = await session.get(BuyingProduct, payload.product_id)
        if not p:
            session.add(BuyingProduct(product_id=payload.product_id, name=payload.name, normalized_name=normalize(payload.name)))
        elif p.normalized_name != normalize(payload.name):
            service.fail("Product already mapped under another name")
        return {"product_id": payload.product_id}


@router.get("/catalog", response_model=CatalogRead)
async def catalog(q: str = Query("", max_length=200), sort: Literal["cheapest", "supplier", "newest"] = "cheapest",
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)) -> dict:
    async with async_session() as session:
        # Page canonical identities, then fetch all their offers in one query.
        pattern = "%" + normalize(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        price = case((SupplierOffer.currency_code == "RUB", SupplierOffer.purchase_price_minor),
                     else_=SupplierOffer.purchase_price_minor * SupplierOffer.current_fx_rate_to_rub)
        base = select(BuyingProduct.product_id).join(SupplierOffer, SupplierOffer.product_id == BuyingProduct.product_id).join(BuyingOffer, BuyingOffer.offer_id == SupplierOffer.id).join(Supplier, Supplier.id == SupplierOffer.supplier_id).where(
            or_(BuyingProduct.normalized_name.like(pattern, escape="\\"), func.lower(BuyingOffer.name).like(pattern, escape="\\")), SupplierOffer.active.is_(True), Supplier.status == "active").group_by(BuyingProduct.product_id)
        order = func.min(price).asc().nullslast() if sort == "cheapest" else func.min(Supplier.name) if sort == "supplier" else func.max(SupplierOffer.valid_at).desc().nullslast()
        total = await session.scalar(select(func.count()).select_from(base.subquery()))
        ids = list((await session.scalars(base.order_by(order, BuyingProduct.product_id).offset(offset).limit(limit))).all())
        products = {p.product_id: dict(id=p.product_id, name=p.name, offers=[]) for p in (await session.scalars(select(BuyingProduct).where(BuyingProduct.product_id.in_(ids)))).all()}
        for o,m,s in (await session.execute(service.offer_query().where(SupplierOffer.product_id.in_(ids), SupplierOffer.active.is_(True), Supplier.status == "active").order_by(price.asc().nullslast(), SupplierOffer.id))).all():
            products[o.product_id]["offers"].append(service.offer_view(o,m,s))
        return {"products": [products[i] for i in ids], "total": total, "offset": offset, "limit": limit}


@router.get("/products/{product_id}/offers", response_model=OffersRead)
async def product_offers(product_id: str) -> dict:
    async with async_session() as session:
        rows = (await session.execute(service.offer_query().where(SupplierOffer.product_id == product_id, SupplierOffer.active.is_(True), Supplier.status == "active"))).all()
        return {"offers": [service.offer_view(*r) for r in rows]}


@router.get("/offers/{offer_id}/price-history")
async def price_history(offer_id: int, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)) -> dict:
    async with async_session() as session:
        if not await session.get(BuyingOffer, offer_id):
            service.fail("Offer not found", 404)
        rows = (await session.scalars(select(BuyingPriceHistory).where(BuyingPriceHistory.offer_id == offer_id).order_by(BuyingPriceHistory.id.desc()).offset(offset).limit(limit))).all()
        return {"history": [{k: getattr(r,k) for k in ("id", "offer_id", "old_price_minor", "new_price_minor", "currency", "import_id", "changed_at")} for r in rows]}


@router.post("/offers/{offer_id}/estimate")
async def estimate(offer_id: int) -> dict:
    async with async_session() as session:
        if not await session.get(BuyingOffer, offer_id):
            service.fail("Offer not found", 404)
        offer = await session.get(SupplierOffer, offer_id)
        currency = offer.currency_code
    try:
        quote = await run_in_threadpool(CbrFxProvider().quote, currency)
    except FxError as error:
        raise HTTPException(503, "FX provider unavailable") from error
    async with async_session() as session, session.begin():
        await service.lock(session)
        offer = await session.get(SupplierOffer, offer_id, with_for_update=True)
        offer.current_fx_rate_to_rub, offer.fx_source, offer.fx_rate_date = quote.rate, quote.source, quote.rate_date
        return {"offer_id": offer.id, "fx_rate_to_rub": str(quote.rate), "fx_source": quote.source, "fx_rate_date": quote.rate_date}


class Quantity(Body):
    quantity: int = Field(strict=True, gt=0, le=100000)


class CartAdd(Quantity):
    offer_id: int = Field(strict=True, gt=0)


@router.get("/cart", response_model=CartRead)
async def cart() -> dict:
    async with async_session() as session:
        return {"items": await service.cart_view(session)}


@router.post("/cart/items")
async def add_cart(payload: CartAdd) -> dict:
    async with async_session() as session, session.begin():
        await service.lock(session)
        if not await session.get(BuyingOffer, payload.offer_id):
            service.fail("Offer not found", 404)
        offer = await session.get(SupplierOffer, payload.offer_id)
        item = await session.get(BuyingCart, payload.offer_id)
        if item:
            item.quantity = payload.quantity
        else:
            session.add(BuyingCart(offer_id=offer.id, quantity=payload.quantity, added_price_minor=offer.purchase_price_minor))
        return {"offer_id": offer.id, "quantity": payload.quantity}


@router.patch("/cart/items/{offer_id}")
async def update_cart(offer_id: int, payload: Quantity) -> dict:
    async with async_session() as session, session.begin():
        await service.lock(session)
        item = await session.get(BuyingCart, offer_id)
        if not item:
            service.fail("Cart item not found", 404)
        item.quantity = payload.quantity
        return {"offer_id": offer_id, "quantity": payload.quantity}


@router.delete("/cart/items/{offer_id}")
async def remove_cart(offer_id: int) -> dict:
    async with async_session() as session, session.begin():
        await service.lock(session)
        await session.execute(delete(BuyingCart).where(BuyingCart.offer_id == offer_id))
        return {"removed": True}


@router.delete("/cart")
async def clear_cart() -> dict:
    async with async_session() as session, session.begin():
        await service.lock(session)
        await session.execute(delete(BuyingCart))
        return {"cleared": True}


@router.post("/checkout/preview", response_model=CheckoutPreviewRead)
async def checkout_preview() -> dict:
    async with async_session() as session, session.begin():
        await service.lock(session)
        return await service.checkout_preview(session)


class CheckoutConfirm(Body):
    fingerprint: str = Field(pattern="^[0-9a-f]{64}$")


@router.post("/checkout/confirm")
async def checkout_confirm(payload: CheckoutConfirm, idempotency_key: str = Header(min_length=8, max_length=100), actor: str = Depends(session_access)) -> dict:
    async with async_session() as session, session.begin():
        ids = await service.checkout_confirm(session, idempotency_key, payload.fingerprint, actor)
        return {"purchase_ids": ids, "real_email_sent": False}


@router.get("/purchases", response_model=PurchasesRead)
async def purchases(supplier_id: int | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)) -> dict:
    async with async_session() as session:
        query = select(BuyingPurchase)
        if supplier_id is not None:
            query = query.where(BuyingPurchase.supplier_id == supplier_id)
        total = await session.scalar(select(func.count()).select_from(query.subquery()))
        rows = (await session.scalars(query.order_by(BuyingPurchase.id.desc()).offset(offset).limit(limit))).all()
        return {"purchases": [service.purchase_view(p) for p in rows], "total": total, "offset": offset, "limit": limit}


@router.get("/purchases/{purchase_id}", response_model=PurchaseDetailRead)
async def purchase_detail(purchase_id: int) -> dict:
    async with async_session() as session:
        purchase = await service.purchase_get(session, purchase_id)
        replies = (await session.scalars(select(BuyingReply).where(BuyingReply.purchase_id == purchase_id).order_by(BuyingReply.received_at))).all()
        return {**service.purchase_view(purchase), "replies": [{k: getattr(r,k) for k in ("message_id", "thread_id", "received_at", "subject", "body", "attachments")} for r in replies]}


@router.post("/purchases/{purchase_id}/simulate-send", response_model=PurchaseRead)
async def simulate_send(purchase_id: int, actor: str = Depends(session_access)) -> dict:
    if settings.environment == "production":
        raise HTTPException(403, "Simulation is only available outside production")
    async with async_session() as session, session.begin():
        p = await service.simulate_send(session, purchase_id, actor)
        await session.flush()
        await session.refresh(p)
        return service.purchase_view(p)


@router.post("/purchases/{purchase_id}/received", response_model=PurchaseRead)
async def received(purchase_id: int, actor: str = Depends(session_access)) -> dict:
    async with async_session() as session, session.begin():
        p = await service.receive(session, purchase_id, actor)
        await session.flush()
        await session.refresh(p)
        return service.purchase_view(p)
