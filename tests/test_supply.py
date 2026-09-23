from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import pytest
from pydantic import ValidationError
from app.services.fx import CbrFxProvider, FakeFxProvider, FxError, FxQuote, convert_minor, rate_value
from app.services.supply_service import x_calculation, SupplyError, LABELS, TRANSITIONS
from app.services.stock_allocation import allocate, StockAllocationError
from app.api.supply import OfferInput, SourceInput
from app.schemas.checkout import PublicProduct


@pytest.mark.parametrize("base,sale,partner,ours,due", [(200000,300000,50000,50000,250000), (200000,300001,50000,50001,250000), (0,1,0,1,0), (200,200,0,0,200)])
def test_x_split(base, sale, partner, ours, due):
    result = x_calculation(base, sale)
    assert (result['partner_margin_minor'],result['our_margin_minor'],result['partner_due_minor']) == (partner,ours,due)
    assert result['status'] == 'calculated'


def test_x_negative_review_has_no_payable():
    result = x_calculation(200000,199999)
    assert result['status'] == 'requires_financial_review'
    assert result['partner_due_minor'] is result['our_margin_minor'] is None


@pytest.mark.parametrize("value", [1.0, True, -1, "200", 2**63])
def test_x_no_float_or_invalid_money(value):
    with pytest.raises(SupplyError):
        x_calculation(value, 500)


@pytest.mark.parametrize("value", [1.0, True, -1, "4250", 2**63])
def test_conversion_no_float_money(value):
    with pytest.raises(FxError):
        convert_minor(value, Decimal('90'))


@pytest.mark.parametrize("value", [1.5, "NaN", "Infinity", "0", "-1", "1.00000000001", "1000000"])
def test_invalid_rate(value):
    with pytest.raises(FxError):
        rate_value(value)


def test_cbr_nominal_and_decimal_conversion():
    xml = b'<ValCurs Date="15.09.2026"><Valute><CharCode>USD</CharCode><Nominal>10</Nominal><Value>905,1234</Value></Valute></ValCurs>'
    quote = CbrFxProvider.parse(xml, 'USD')
    assert quote.rate == Decimal('90.51234')
    assert quote.rate_date == date(2026,9,15)
    assert convert_minor(4250, quote.rate) == 384677
    assert convert_minor(1, Decimal('1.5')) == 2


@pytest.mark.parametrize("payload", [b'garbage', b'<!DOCTYPE unsafe><ValCurs/>', b'<ValCurs Date="bad"/>'])
def test_cbr_malformed_controlled(payload):
    with pytest.raises(FxError):
        CbrFxProvider.parse(payload, 'USD')


@pytest.mark.parametrize("currency", ['RUB','USD','EUR','CNY'])
def test_currency_extension(currency):
    quote = FakeFxProvider('90').quote(currency)
    assert convert_minor(4250,quote.rate) == (4250 if currency == 'RUB' else 382500)


def test_manual_rub_cannot_change_identity_rate():
    with pytest.raises(FxError):
        FxQuote('RUB', Decimal('90'), 'manual', date.today())


@pytest.mark.parametrize("state", ['on_request','confirmed'])
def test_external_bucket_has_no_physical_allocation(state):
    assert LABELS['external'] == 'Внешние поставщики'
    assert allocate([{'id':'p','qty':3}], [{'id':'p','supply_source':'external','supply_availability':state,'stocks':[]}]) == []


def test_external_unavailable_rejected():
    with pytest.raises(StockAllocationError):
        allocate([{'id':'p','qty':1}], [{'id':'p','supply_source':'external','supply_availability':'unavailable'}])


def test_offer_api_rejects_float_and_privileged_input():
    payload = dict(product_id='p',supplier_id=1,purchase_price_minor=4250,currency_code='USD')
    assert OfferInput(**payload).purchase_price_minor == 4250
    with pytest.raises(ValidationError):
        OfferInput(**dict(payload,purchase_price_minor=42.5))
    with pytest.raises(ValidationError):
        OfferInput(**dict(payload,our_margin_minor=10))
    with pytest.raises(ValidationError):
        SourceInput(source_type='own,partner_x')


def test_public_finance_never_serialized():
    public = PublicProduct(id='p',name='p',price_minor=None,available=False,requires_review=True,
        supplier_id=1,purchase_price_minor=4250,fx_rate_to_rub='90',margin=100,partner_due=99)
    assert set(public.model_dump()) == {'id','name','article','description','price_minor','currency','available','requires_review','availability_state'}


def test_procurement_cannot_skip_or_reopen_confirmed_cost():
    assert 'confirmed' not in TRANSITIONS['needed']
    assert TRANSITIONS['received'] == set()
    assert 'requested' not in TRANSITIONS['confirmed']


def test_client_runtime_never_registers_procurement_manager_router():
    from pathlib import Path
    assert 'app.bot.procurement' not in Path('app/workers/client_bot.py').read_text(encoding='utf-8')


def test_cbr_network_timeout_controlled(monkeypatch):
    import requests
    from unittest.mock import MagicMock
    session = MagicMock()
    session.__enter__.return_value = session
    session.get.side_effect = requests.Timeout()
    monkeypatch.setattr('app.services.fx.verified_session', lambda: session)
    with pytest.raises(FxError, match='ЦБ временно'):
        CbrFxProvider().quote('USD')
    assert session.get.call_args.kwargs['timeout'] == (5,15)


def test_unauthorized_manager_procurement_command(monkeypatch):
    from app.bot.telegram_bot import dp
    # The service also enforces actor identity; handler gate rejects before any access.
    from app.bot.procurement import register
    handlers = []
    class Observer:
        def __call__(self,*args):
            def deco(fn):
                handlers.append(fn)
                return fn
            return deco
    fake = SimpleNamespace(message=Observer(),callback_query=Observer())
    register(fake,AsyncMock(return_value=False),AsyncMock(),SimpleNamespace())
    message = SimpleNamespace(answer=AsyncMock())
    asyncio.run(next(h for h in handlers if h.__name__ == 'manual_fx')(message))
    message.answer.assert_not_awaited()


def test_supply_api_missing_token_fails_closed(monkeypatch):
    from app.main import app
    from app.config.settings import settings
    monkeypatch.setattr(settings,'internal_api_token','synthetic-test-internal-key')
    async def request(path):
        sent = []
        async def receive():
            return {'type':'http.request','body':b'','more_body':False}
        async def send(event):
            sent.append(event)
        await app({'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':'GET',
            'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'product_id=p',
            'headers':[],'client':('127.0.0.1',123),'server':('test',80),'root_path':''},receive,send)
        return next(e['status'] for e in sent if e['type']=='http.response.start')
    for path in ('/internal/supply/suppliers','/internal/supply/offers'):
        assert asyncio.run(request(path)) == 401


def test_manual_override_is_decimal_string_in_openapi():
    from app.main import app
    schema = app.openapi()['components']['schemas']
    assert schema['ProcurementAction']['properties']['manual_rate']['anyOf'][0]['type'] == 'string'
    assert schema['OfferInput']['properties']['purchase_price_minor']['type'] == 'integer'


def test_no_impossible_assembly_button_waiting_for_external():
    from app.services.manager_workspace import order_keyboard
    order = SimpleNamespace(id=1,revision=0,fulfillment_status='new',status='new',needs_review=False,
        payment_status='unpaid',delivery_method='unselected',delivery_status='pending',
        supply_has_external=True,supply_received=False,supply_external_only=True)
    callbacks = [b['callback_data'] for row in order_keyboard(order)['inline_keyboard'] for b in row]
    assert not any(':assembling:' in c or ':allocate:' in c for c in callbacks)
    assert any(':procurement:' in c for c in callbacks)


def test_offer_currency_change_requires_new_offer(monkeypatch):
    from app.api import supply
    from fastapi import HTTPException
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        def begin(self): return self
        async def get(self,*args,**kwargs):
            return SimpleNamespace(product_id='p',supplier_id=1,currency_code='USD')
    monkeypatch.setattr(supply,'async_session',Session)
    with pytest.raises(HTTPException) as error:
        asyncio.run(supply.offer_update(1,OfferInput(product_id='p',supplier_id=1,purchase_price_minor=100,currency_code='EUR')))
    assert error.value.status_code == 409
