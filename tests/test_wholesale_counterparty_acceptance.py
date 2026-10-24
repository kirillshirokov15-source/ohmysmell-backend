"""Counterparty reconciliation and narrow, manager-approved create acceptance.

All provider requests are fake; production external writes are never made.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config.settings import settings
from app.models.customer import Customer
from app.models.sales import CustomerType
from app.repositories.draft_order_repository import DraftOrderRepository
from app.services.counterparty_matching_service import CounterpartyMatchingService
from app.services.counterparty_creation_service import CounterpartyCreationService, CounterpartyCreationError
from app.services.draft_telegram_service import build_draft_card, build_draft_keyboard
from app.services.draft_order_service import DraftOrderService, DraftOrderError
from app.services.product_matching_service import ProductMatchingService
from app.services.order_lifecycle import InvalidOrderTransitionError
from app.integrations.write_guard import require_counterparty_creation, require_external_writes, ExternalWritesDisabled
from tests.test_tilda_sku_review import database, catalog, tilda_config


class Provider:
    def __init__(self, rows=None):
        self.rows=list(rows or [])
        self.calls=[]
        self.create_calls=0
    def search_counterparties(self, query):
        self.calls.append(("search",query))
        q=str(query).lower()
        return [r for r in self.rows if any(q in str(r.get(k) or "").lower()
                for k in ("name","email","phone","inn"))]
    def find_counterparties_exact(self, field, value):
        self.calls.append(("exact",field,value))
        return [r for r in self.rows if str(r.get(field) or "").casefold()==str(value).casefold()]
    def get_counterparty(self, identity):
        return next(r for r in self.rows if r["id"]==identity)
    def create_counterparty(self, payload):
        self.create_calls+=1
        identity="new-uuid-001"
        row={"id":identity,"name":payload["name"],"email":payload["email"],"externalCode":payload["externalCode"]}
        self.rows.append(row)
        return row
    def get_recent_counterparties(self, limit=10):
        raise AssertionError("Unrelated recent-counterparty fallback must not be used")


class FakeOperations:
    def __init__(self, provider):
        self.provider=provider
        self.saved={}
        self.store=SimpleNamespace(get=self.get)
    async def get(self, key):
        return self.saved.get(key)
    async def execute_counterparty(self,key,payload,send):
        if key in self.saved:
            assert self.saved[key]["payload"]==payload
            return self.saved[key]["result"]
        result=await send(payload)
        self.saved[key]={"status":"succeeded","payload":payload,"result":result}
        return result


def message(customer_id, name="Client", email="buyer@example.com", source_intent="order", provider_id=None):
    return {
        "source":"email","external_message_id":f"cp-accept-{customer_id}",
        "sender_email":email,"customer_name":name,
        "subject":"Заказ","body_text":"Marvis 1 шт",
        "received_at":datetime.now(timezone.utc),
        "customer_id":customer_id,"customer_type":CustomerType.WHOLESALE,
        "contact_details":{"mail_intent":source_intent},
        "counterparty_id":provider_id,"counterparty_candidates":[],
        "status":"needs_review","total":None,
        "problems":["Нужно сверить контрагента"],"items":[],
    }


def test_lookup_checks_all_queries_and_ranks_exact_identity():
    records=[
        {"id":"name","name":"Иван Петров","email":"not-mine@example.org"},
        {"id":"mail","name":"Компания Петрова","email":"BUYER@EXAMPLE.COM"},
        {"id":"maybe","name":"Иванова","email":"other@example.org"},
    ]
    provider=Provider(records)
    matcher=CounterpartyMatchingService(provider)
    async def run():
        found=await matcher.candidates_async("buyer@example.com","Иван Петров")
        assert [x["id"] for x in found]==["mail","name"]
        assert found[0]["reason"].startswith("Email")
        assert found[1]["reason"].startswith("Название")
        assert len([x for x in provider.calls if x[0]=="search"])==2
        assert await matcher.candidates_async("nomatch@example.net","Missing Company")==[]
        with pytest.raises(ValueError):
            await matcher.verify_counterparty("maybe",email="buyer@example.com",name="Иван Петров")
    asyncio.run(run())


def test_duplicate_scan_uses_exact_filters_and_no_silent_create():
    provider=Provider([{"id":"some","name":"Ромашка ООО","email":"owner@example.com"}])
    matcher=CounterpartyMatchingService(provider)
    async def run():
        results=await matcher.duplicate_candidates(name="Ромашка ООО",email="new@example.com")
        assert len(results)==1
        assert ("exact","name","Ромашка ООО") in provider.calls
        assert ("exact","email","new@example.com") in provider.calls
        assert results[0]["id"]=="some"
    asyncio.run(run())


def test_narrow_guard_independent_of_stock_write(monkeypatch):
    monkeypatch.setattr(settings,"environment","production")
    monkeypatch.setattr(settings,"external_writes_enabled",False)
    monkeypatch.setenv("OMS_COUNTERPARTY_CREATION_ENABLED","true")
    require_counterparty_creation()
    with pytest.raises(ExternalWritesDisabled):
        require_external_writes()
    monkeypatch.setattr(settings,"environment","staging")
    with pytest.raises(ExternalWritesDisabled):
        require_counterparty_creation()


def test_manager_preview_create_once_and_crm_link(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    monkeypatch.setattr(settings,"environment","production")
    monkeypatch.setattr(settings,"external_writes_enabled",False)
    monkeypatch.setenv("OMS_COUNTERPARTY_CREATION_ENABLED","true")
    repo=DraftOrderRepository()
    provider=Provider()
    matcher=CounterpartyMatchingService(provider)
    operations=FakeOperations(provider)
    service=CounterpartyCreationService(matcher,operations,repo)

    async def run():
        async with database() as s,s.begin():
            customer=Customer(customer_type=CustomerType.WHOLESALE,display_name="Customer")
            s.add(customer)
            await s.flush()
            cid=customer.id
        draft=await repo.create(message(cid))
        preview=await service.preview(draft.id,"ООО Омега",111)
        assert preview.contact_details["counterparty_create_preview"]["name"]=="ООО Омега"
        assert "ручного поиска" in build_draft_card(preview)
        assert provider.create_calls==0
        result=await service.create_confirmed(draft.id,111)
        assert result.counterparty_id=="new-uuid-001"
        assert provider.create_calls==1
        loaded=await repo.get(draft.id)
        assert loaded.contact_details["counterparty_reconciliation"]["evidence"]=="created_after_duplicate_check"
        assert loaded.counterparty_id=="new-uuid-001"
        assert "counterparty_create_preview" not in loaded.contact_details
        assert len((await repo.list(source="email")))==1
        with pytest.raises(CounterpartyCreationError):
            await service.create_confirmed(draft.id,111)
        assert provider.create_calls==1
        with pytest.raises(ExternalWritesDisabled):
            require_external_writes()
    asyncio.run(run())


def test_existing_counterparty_is_not_linked_to_another_customer(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    repo=DraftOrderRepository()
    async def run():
        async with database() as s,s.begin():
            a=Customer(customer_type=CustomerType.WHOLESALE,moysklad_counterparty_id="cp-A")
            b=Customer(customer_type=CustomerType.WHOLESALE)
            s.add_all([a,b]);await s.flush()
            aid,bid=a.id,b.id
        draft=await repo.create(message(bid))
        with pytest.raises(InvalidOrderTransitionError,match="другому клиенту"):
            await repo.set_counterparty(draft.id,"cp-A","ООО А")
        reread=await repo.get(draft.id)
        assert reread.counterparty_id is None
    asyncio.run(run())


def test_manual_counterparty_selection_reads_current_moysklad(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    repo=DraftOrderRepository()
    record={"id":"legal-uuid-1","name":"ООО ТЕСТ","email":"buyer@example.com"}
    provider=Provider([record])
    matcher=CounterpartyMatchingService(provider)
    from types import SimpleNamespace as SN
    service=DraftOrderService(repository=repo,counterparty_service=matcher,
        matching_service=ProductMatchingService(provider=SN(get_products=lambda:[])))
    async def run():
        async with database() as s,s.begin():
            customer=Customer(customer_type=CustomerType.WHOLESALE)
            s.add(customer);await s.flush()
            cid=customer.id
        draft=await repo.create(message(cid))
        selected=await service.load_counterparty_candidates(draft.id)
        assert selected.counterparty_candidates[0]["reason"].startswith("Email")
        record["archived"]=True
        with pytest.raises(DraftOrderError,match="изменился"):
            await service.link_counterparty(draft.id,"legal-uuid-1","ООО ТЕСТ",actor=111)
        record["archived"]=False
        result=await service.link_counterparty(draft.id,"legal-uuid-1","ООО ТЕСТ",actor=111)
        assert result.counterparty_id=="legal-uuid-1"
        assert result.contact_details["counterparty_reconciliation"]["actor"]==111
    asyncio.run(run())


def test_active_archive_duplicate_cannot_be_recreated():
    provider=Provider([{"id":"archived-uuid","name":"ООО ОМЕГА",
                        "email":"buyer@example.com","archived":True}])
    matcher=CounterpartyMatchingService(provider)
    async def run():
        results=await matcher.duplicate_candidates(name="ООО ОМЕГА",
                                                   email="buyer@example.com")
        assert results
        assert "Архивная" in results[0]["reason"]
    asyncio.run(run())


def test_manager_business_name_search_and_counterparty_reconciliation(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    repo=DraftOrderRepository()
    provider=Provider([{"id":"cp-verify","name":"ООО Золотая Роза",
                        "email":"other@example.com","inn":"7812345678",
                        "phone":"+7 999 111-22-33"}])
    matcher=CounterpartyMatchingService(provider)
    from types import SimpleNamespace as SN
    service=DraftOrderService(repository=repo,counterparty_service=matcher,
        matching_service=ProductMatchingService(provider=SN(get_products=lambda:[])))
    async def run():
        async with database() as s,s.begin():
            customer=Customer(customer_type=CustomerType.WHOLESALE)
            s.add(customer);await s.flush()
            cid=customer.id
        draft=await repo.create(message(cid,name="Buyer Person"))
        candidates=await service.search_counterparties(draft.id,"Золотая Роза")
        assert len(candidates.counterparty_candidates)==1
        assert "Поиск менеджера" in candidates.counterparty_candidates[0]["reason"]
        fresh=await service.counterparty_reconciliation(draft.id)
        assert "Контрагент CRM: пока не выбран" in fresh
        assert "Ввести контрагента вручную" in fresh
        selected=await service.link_counterparty(
            draft.id,"cp-verify","ООО Золотая Роза",actor=111)
        assert selected.counterparty_id=="cp-verify"
        report=await service.counterparty_reconciliation(draft.id)
        assert "ИНН МойСклад: 7812345678" in report
        assert "Email: ⚠ отличается" in report
        assert "Поиск менеджера" in report
    asyncio.run(run())


def test_counterparty_transport_rejects_disabled_creation(monkeypatch):
    from app.integrations.moysklad.client import MoySkladClient
    from unittest.mock import Mock
    monkeypatch.setattr("app.integrations.moysklad.client.MOYSKLAD_TOKEN", "DUMMY_FAKE_NO_NETWORK")
    monkeypatch.setattr(settings,"environment","production")
    monkeypatch.setenv("OMS_COUNTERPARTY_CREATION_ENABLED","false")
    session=Mock()
    client=MoySkladClient(session=session)
    with pytest.raises(ExternalWritesDisabled):
        client.create_counterparty({"name":"ООО ОМЕГА","email":"person@example.com",
            "externalCode":"oms:counterparty:customer:4"})
    session.post.assert_not_called()


def test_preview_does_not_allow_duplication(database,monkeypatch):
    monkeypatch.setattr("app.repositories.draft_order_repository.async_session",database)
    repo=DraftOrderRepository()
    provider=Provider([{"id":"existing","name":"ООО ОМЕГА","email":"someone@example.com"}])
    service=CounterpartyCreationService(CounterpartyMatchingService(provider),
                                        FakeOperations(provider),repo)
    async def run():
        async with database() as s,s.begin():
            customer=Customer(customer_type=CustomerType.WHOLESALE)
            s.add(customer);await s.flush()
            cid=customer.id
        draft=await repo.create(message(cid))
        with pytest.raises(CounterpartyCreationError,match="Существующие|Похожие|существующие"):
            await service.preview(draft.id,"ООО ОМЕГА",111)
        assert provider.create_calls==0
    asyncio.run(run())
