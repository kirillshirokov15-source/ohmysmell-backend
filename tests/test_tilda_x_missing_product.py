"""Tilda #138: a missing X SKU can be cataloged without replacing item 1.

All provider calls are fake; no real MoySklad inventory movements.
"""
import asyncio
from types import SimpleNamespace as Obj
from unittest.mock import AsyncMock

import pytest

from app.config.settings import settings
from app.models.draft_order import DraftOrder
from app.services.tilda_x_product_service import (
    TildaXProductService, x_product_key, x_product_payload,
)
from app.services.order_desk import TildaIntake, DeskService, DeskError
from tests.test_tilda_sku_review import database, catalog, tilda_config, get_draft, payload


def two_items():
    data = payload()
    data["items"].append({
        "externalid": "CT-X-999",
        "name": "Charlotte Tilbury Unique X Spray 200ml",
        "qty": 1, "price_minor": 350000,
    })
    data["reported_total"] += 350000
    return data


def test_product_intent_uses_stable_sku_only_and_not_stock():
    a = x_product_payload(two_items()["items"][1])
    assert a["article"] == "CT-X-999"
    assert a["name"] == "Charlotte Tilbury Unique X Spray 200ml"
    assert a["externalCode"] == "oms-tilda-x-" + x_product_key("CT-X-999")
    assert not any(k in a for k in ("stock","reserve","supply","quantity","salePrices","buyPrice"))
    assert x_product_key(" CT-X-999 ") == x_product_key("CT-X-999")


class NoNetworkCatalog:
    def __init__(self):
        self.rows=[{"id":"ms-dsd","article":"DSD020","name":"Catalog name","code":"20"}]
        self.created=[]
    def get_products(self):
        return list(self.rows)
    def get_product(self, uid):
        return next(p for p in self.rows if p["id"]==uid)
    def create_x_catalog_product(self, body):
        self.created.append(body)
        item={**body,"id":"new-x-product"}
        self.rows.append(item)
        return item


def prepare(monkeypatch, factory, catalog):
    from app.services import tilda_x_product_service as x
    client = NoNetworkCatalog()
    monkeypatch.setattr(x,"async_session",factory)
    monkeypatch.setattr(settings,"environment","production")
    monkeypatch.setattr(settings,"external_writes_enabled",True)
    monkeypatch.setenv("OMS_OWN_X_PRODUCT_CREATE_ENABLED","true")
    operations = Obj(execute=AsyncMock(side_effect=lambda key,payload,send:send(payload)))
    matcher = AsyncMock(side_effect=lambda: client.get_products())
    monkeypatch.setattr("app.services.product_matching_service.ProductMatchingService.products_async",matcher)
    return TildaXProductService(sessions=factory,client=client,operations=operations),client,operations


def test_missing_x_product_is_created_only_after_confirm_and_preserves_other_item(database,catalog,monkeypatch):
    async def run():
        did = (await TildaIntake().submit(two_items()))["draft_id"]
        await DeskService().claim(did,111,-100123456)
        item_before=sorted((await get_draft(database,did)).items,key=lambda i:i.id)
        assert item_before[0].product_id=="ms-dsd" and item_before[1].product_id is None
        svc,provider,ops=prepare(monkeypatch,database,catalog)
        preview=await svc.preview(did,2,111,-100123456)
        assert preview["sku"]=="CT-X-999" and preview["position"]==2
        assert provider.created==[] and ops.execute.await_count==0
        result=await svc.create_and_bind(did,2,111,-100123456,preview["revision"])
        assert result["revision"]==1
        assert len(provider.created)==1 and ops.execute.await_count==1
        draft=await get_draft(database,did)
        ordered=sorted(draft.items,key=lambda i:i.id)
        assert ordered[0].product_id=="ms-dsd"
        assert ordered[1].product_id=="new-x-product"
        assert ordered[1].article=="CT-X-999"
        assert ordered[1].price==350000 and ordered[1].qty==1
        assert draft.total==518000
        assert draft.contact_details["tilda_manual_matches"]=={"1":"new-x-product"}
        with pytest.raises(DeskError,match="уже сопоставлен"):
            await svc.create_and_bind(did,2,111,-100123456,1)
        assert len(provider.created)==1
    asyncio.run(run())


def test_existing_similar_catalog_product_blocks_x_creation(database,catalog,monkeypatch):
    async def run():
        did = (await TildaIntake().submit(two_items()))["draft_id"]
        await DeskService().claim(did,111,-100123456)
        svc,provider,ops=prepare(monkeypatch,database,catalog)
        provider.rows.append({"id":"other","name":"Charlotte Tilbury Unique X Spray 200ml","article":"OTHER"})
        with pytest.raises(DeskError,match="дубль"):
            await svc.preview(did,2,111,-100123456)
        assert provider.created==[]
        assert ops.execute.await_count==0
    asyncio.run(run())


def test_existing_archived_sku_blocks_recreation(database,catalog,monkeypatch):
    async def run():
        did=(await TildaIntake().submit(two_items()))["draft_id"]
        await DeskService().claim(did,111,-100123456)
        svc,provider,ops=prepare(monkeypatch,database,catalog)
        provider.rows.append({"id":"archived","name":"other","article":"CT-X-999","archived":True})
        with pytest.raises(DeskError,match="дубль"):
            await svc.preview(did,2,111,-100123456)
        assert provider.created==[] and ops.execute.await_count==0
    asyncio.run(run())


def test_no_existing_product_creation_when_not_explicitly_enabled(database,catalog,monkeypatch):
    async def run():
        did=(await TildaIntake().submit(two_items()))["draft_id"]
        await DeskService().claim(did,111,-100123456)
        svc,provider,ops=prepare(monkeypatch,database,catalog)
        monkeypatch.setenv("OMS_OWN_X_PRODUCT_CREATE_ENABLED","false")
        with pytest.raises(DeskError,match="не включено"):
            await svc.create_and_bind(did,2,111,-100123456,0)
        assert ops.execute.await_count==0 and provider.created==[]
    asyncio.run(run())
