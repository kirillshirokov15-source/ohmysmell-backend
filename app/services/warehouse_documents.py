"""Receipts, reservations and shipment documents on existing source plans.

Fake and live intent journals are separated. No operation retries an uncertain
provider create. Order locking freezes the allocation snapshot during each call.
"""
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from app.database.session import async_session
from app.config.settings import settings
from app.models.fulfillment import ExternalOperation
from app.models.operations import OrderEvent
from app.models.order import Order
from app.models.order_desk import OrderDesk
from app.models.supply import Supplier, XSettlement
from app.services.order_source_service import OrderSourceService, source_plans, allocation_ready
from app.services.order_desk import DeskError, authorized_manager, require_owner
from app.services.external_operation_service import ExternalOperationService
from app.integrations.moysklad.documents import FakeDocumentAdapter, MoySkladDocumentAdapter
from app.integrations.moysklad.payloads import reservation, supply, demand
from app.integrations.write_guard import require_external_writes


def plan_generation(plan):
    return hashlib.sha256(json.dumps(sorted((a.id,a.order_item_id,a.qty) for a in plan.allocations
        if a.supply_status!='cancelled')).encode()).hexdigest()[:16]


def plan_key(plan, action, fake):
    return ('simulation:' if fake else '')+f'warehouse:{plan.id}:{plan_generation(plan)}:{action}'


async def frozen_plan(session, plan):
    for fake in (False,True):
        for a in plan.allocations:
            receipt=await session.get(ExternalOperation,('simulation:' if fake else '')+f'warehouse:allocation:{a.id}:receipt')
            # Posted goods cannot be silently freed and received a second time.
            # Returns/reassignment after posting require an explicit inventory workflow.
            if receipt:
                return True
        reserved=await session.get(ExternalOperation,plan_key(plan,'reserve',fake))
        released=await session.get(ExternalOperation,plan_key(plan,'release',fake))
        if reserved and not (released and released.status=='succeeded'):
            return True
    return False


class WarehouseDocumentService:
    def __init__(self, adapter=None, operations=None, sessions=None):
        self.adapter=adapter or MoySkladDocumentAdapter()
        self.operations=operations or ExternalOperationService()
        self.sessions=sessions or async_session

    async def receive_purchase(self, purchase_id, actor, warehouse_id, *, reconcile=False, costs=None):
        """Post an unallocated standalone purchase to stock, once, on the same ledger."""
        from app.services import buying
        from app.models.buying import BuyingEvent
        from app.models.fulfillment import WarehouseAllocation
        from app.services.product_service import ProductService
        fake=type(self.adapter) is FakeDocumentAdapter
        if not fake:
            require_external_writes()
        elif settings.environment=='production':
            raise DeskError('Simulation forbidden in production')
        key=('simulation:' if fake else '')+f'buying:purchase:{purchase_id}:receipt'
        async with self.sessions() as session, session.begin():
            await buying.lock(session)
            purchase=await buying.purchase_get(session,purchase_id)
            if purchase.status!='received':
                raise DeskError('Physical purchase receipt must be recorded first')
            prior=purchase.external_ids.get('warehouse_receipt')
            other_key=key.removeprefix('simulation:') if fake else 'simulation:'+key
            if (prior and (prior['warehouse_id']!=warehouse_id or prior['simulated']!=fake)) or await session.get(ExternalOperation,other_key):
                raise DeskError('Purchase receipt already exists in another mode or warehouse')
            allocated=await session.scalar(select(WarehouseAllocation.id).where(
                WarehouseAllocation.buying_purchase_id==purchase_id,
                (WarehouseAllocation.supply_status!='cancelled') | WarehouseAllocation.receipt_reference.is_not(None)))
            if allocated:
                raise DeskError('Allocated purchase must be posted through its source plans')
            supplier=await session.get(Supplier,purchase.supplier_id)
            if not supplier.external_reference or not settings.moysklad_organization_id:
                raise DeskError('Supplier and organization mappings required')
            catalog={p['id']:p for p in await ProductService().get_catalog_async(include_supply=False)}
            rows=[]
            items=purchase.snapshot['items']
            if costs is not None and (set(costs)!={i['product_id'] for i in items} or
                any(type(v) is not int or not 0<=v<=10**12 for v in costs.values())):
                raise DeskError('Exact RUB receipt costs required for every product')
            if purchase.snapshot['currency']!='RUB' and costs is None:
                raise DeskError('Confirm actual RUB costs; approximate FX is insufficient')
            for item in items:
                product=catalog.get(item['product_id'])
                if (not product or product.get('archived') or
                    warehouse_id not in {s['id'] for s in product.get('stocks',[])} or
                    (settings.warehouse_ids and warehouse_id not in settings.warehouse_ids)):
                    raise DeskError('Verified catalog product and receiving warehouse required')
                rows.append({'id':item['product_id'],'qty':item['quantity'],
                    'price':costs[item['product_id']] if costs is not None else item['unit_price_minor']})
            document=supply(settings.moysklad_organization_id,supplier.external_reference,warehouse_id,
                            rows,key.removeprefix('simulation:'))
            intent={'entity':'supply','document':document}
            if reconcile:
                previous=await self.operations.store.get(key)
                if not previous or previous['payload']!=intent:
                    raise DeskError('Receipt reconciliation parameters changed')
                result=await self.operations.reconcile_document(key,self.adapter)
            elif fake:
                result=await self.operations.simulate_document(key.removeprefix('simulation:'),intent,self.adapter)
            else:
                result=await self.operations.execute(key,intent,self.adapter.create)
            receipt={'id':result['id'],'purchase_id':purchase_id,'warehouse_id':warehouse_id,'simulated':fake}
            if purchase.external_ids.get('warehouse_receipt')!=receipt:
                if purchase.external_ids.get('warehouse_receipt'):
                    raise DeskError('Purchase already received in another mode or warehouse')
                purchase.external_ids={**purchase.external_ids,'warehouse_receipt':receipt}
                session.add(BuyingEvent(purchase_id=purchase_id,action='warehouse_receipt',actor=actor))
            return receipt

    async def act(self, order_id, actor, chat_id, revision, action, target_id, *, reconcile=False, receipt_cost_minor=None):
        if action not in {'receipt','reserve','ship','release'}:
            raise DeskError('Unsupported warehouse document')
        if receipt_cost_minor is not None and (action!='receipt' or type(receipt_cost_minor) is not int
                                              or not 0<=receipt_cost_minor<=10**12):
            raise DeskError('Explicit receipt unit cost must be RUB minor units')
        fake=type(self.adapter) is FakeDocumentAdapter
        if not fake:
            require_external_writes()
        elif settings.environment=='production':
            raise DeskError('Simulation forbidden in production')
        async with self.sessions() as session, session.begin():
            manager=await authorized_manager(session,actor,chat_id)
            order=await session.scalar(select(Order).where(Order.id==order_id)
                .options(selectinload(Order.items)).with_for_update())
            if not order:
                raise DeskError('Order not found')
            if order.manual_fulfillment:
                desk=await session.scalar(select(OrderDesk).where(OrderDesk.order_id==order_id))
                if not desk:
                    raise DeskError('Order desk not found')
                require_owner(desk,actor)
            plans=await source_plans(session,order_id)
            if order.status=='rejected' or order.fulfillment_status=='cancelled' or order.needs_review:
                raise DeskError('Order is unavailable')
            items={i.id:i for i in order.items}
            if action=='receipt':
                pair=next(((p,a) for p in plans for a in p.allocations if a.id==target_id),None)
                if not pair:
                    raise DeskError('Allocation not found')
                plan,a=pair
                key=('simulation:' if fake else '')+f'warehouse:allocation:{a.id}:receipt'
            else:
                plan=next((p for p in plans if p.id==target_id),None)
                if not plan:
                    raise DeskError('Plan not found')
                key=plan_key(plan,action,fake)
            if action=='reserve':
                released=await session.get(ExternalOperation,plan_key(plan,'release',fake))
                if released:
                    raise DeskError('Released reservation requires a new allocation plan')
            previous=await session.scalar(select(OrderEvent).where(OrderEvent.order_id==order_id,
                OrderEvent.idempotency_key==key))
            if previous:
                if previous.before.get('receipt_cost_minor')!=receipt_cost_minor:
                    raise DeskError('Document parameters changed after confirmation')
                return previous.after
            if order.revision!=revision:
                raise DeskError('Карточка устарела')
            if plan.status not in {'planned','exported'} or (plan.status=='exported' and action!='ship'):
                raise DeskError('Plan is unavailable')
            if not settings.moysklad_organization_id:
                raise DeskError('Organization mapping required')
            bare_key=key.removeprefix('simulation:')
            if action=='receipt':
                if order.fulfillment_status!='new' or a.source_type=='own' or a.supply_status!='received':
                    raise DeskError('Actual receipt must be recorded before posting')
                supplier=await session.get(Supplier,a.supplier_id)
                if not supplier or not supplier.external_reference:
                    raise DeskError('Supplier counterparty mapping required')
                item=items[a.order_item_id]
                if a.source_type=='external':
                    p=a.buying_purchase
                    if not p or p.status!='received':
                        raise DeskError('Received purchase required')
                    matching=[i for i in p.snapshot['items'] if i['product_id']==item.product_id]
                    if len(matching)!=1:
                        raise DeskError('Purchase item is ambiguous')
                    if p.snapshot['currency']!='RUB' and receipt_cost_minor is None:
                        raise DeskError('Confirm actual receipt cost in RUB; approximate FX is insufficient')
                    cost=matching[0]['unit_price_minor'] if receipt_cost_minor is None else receipt_cost_minor
                else:
                    settlement=await session.scalar(select(XSettlement).where(XSettlement.allocation_id==a.id))
                    if not settlement or settlement.base_cost_minor % a.qty:
                        raise DeskError('Exact X receipt cost required')
                    cost=settlement.base_cost_minor//a.qty
                    if receipt_cost_minor is not None and receipt_cost_minor!=cost:
                        raise DeskError('X receipt cost must match the approved settlement')
                document=supply(settings.moysklad_organization_id,supplier.external_reference,plan.warehouse_id,
                    [{'id':item.product_id,'qty':a.qty,'price':cost}],bare_key)
                entity='supply'
            else:
                rows=[a for a in plan.allocations if a.supply_status!='cancelled']
                if not rows or not all(allocation_ready(a,manual=order.manual_fulfillment) for a in rows):
                    raise DeskError('All sources must be received and posted')
                for a in rows:
                    if a.source_type!='own':
                        receipt=await session.get(ExternalOperation,('simulation:' if fake else '')+f'warehouse:allocation:{a.id}:receipt')
                        if not receipt or receipt.status!='succeeded' or receipt.result['id']!=a.receipt_reference:
                            raise DeskError('Verified receipt document required')
                positions=[{'id':items[a.order_item_id].product_id,'qty':a.qty,
                            'price':items[a.order_item_id].price} for a in rows]
                if not order.counterparty_id:
                    raise DeskError('Customer counterparty mapping required')
                document=reservation(settings.moysklad_organization_id,order.counterparty_id,
                                     plan.warehouse_id,positions,bare_key)
                entity='customerorder'
                if action in {'ship','release'}:
                    reserve=await session.get(ExternalOperation,plan_key(plan,'reserve',fake))
                    if not reserve or reserve.status!='succeeded':
                        raise DeskError('Verified reservation required')
                    released=await session.get(ExternalOperation,plan_key(plan,'release',fake))
                    if released and released.status=='succeeded':
                        raise DeskError('Reservation already released')
                    if action=='ship':
                        if order.fulfillment_status not in {'assembled','shipped'}:
                            raise DeskError('Assembly must finish before shipment')
                        document=demand(settings.moysklad_organization_id,order.counterparty_id,plan.warehouse_id,
                                        reserve.result['id'],positions,bare_key)
                        document['applicable']=True
                        entity='demand'
                    else:
                        if plan.external_id:
                            raise DeskError('Shipped reservation cannot be released')
                        for row in document['positions']:
                            row['reserve']=0
                        document['applicable']=False
                        document['syncId']=reserve.result['syncId']
                elif action=='reserve':
                    if order.fulfillment_status!='new':
                        raise DeskError('Reserve before assembly')
                    from app.services.order_source_service import lock_source_stock, claimed_stock
                    from app.services.product_service import ProductService
                    from app.services.stock_allocation import available_units
                    own={}
                    for a in rows:
                        if a.source_type=='own':
                            pid=items[a.order_item_id].product_id
                            own[pid]=own.get(pid,0)+a.qty
                    await lock_source_stock(session,[(pid,plan.warehouse_id) for pid in own])
                    if own:
                        catalog={p['id']:p for p in await ProductService().get_catalog_async(include_supply=False)}
                        for pid in own:
                            stock=next((s for s in catalog.get(pid,{}).get('stocks',[]) if s['id']==plan.warehouse_id),None)
                            claimed=await claimed_stock(session,pid,plan.warehouse_id)
                            if not stock or available_units(stock.get('stock'),stock.get('reserve'))<claimed:
                                raise DeskError('Fresh stock no longer covers local allocations')
            intent={'entity':entity,'document':document}
            if action=='release':
                intent['update_id']=reserve.result['id']
            if reconcile:
                recorded=await self.operations.store.get(key)
                if not recorded or recorded['payload']!=intent:
                    raise DeskError('Reconciliation intent differs from recorded operation')
                result=await self.operations.reconcile_document(key,self.adapter)
            elif fake:
                result=await self.operations.simulate_document(bare_key,intent,self.adapter)
            else:
                result=await self.operations.execute(key,intent,self.adapter.create)
            if action=='receipt':
                a.supply_status='confirmed'; a.receipt_reference=result['id']; a.receipt_simulated=fake
            elif action=='ship':
                plan.external_id=result['id']; plan.status='exported'
            order.revision+=1
            after={'id':result['id'],'revision':order.revision,'action':action,'simulated':fake}
            session.add(OrderEvent(order_id=order_id,manager_id=manager.id,action='warehouse_'+action,
                idempotency_key=key,request_hash=hashlib.sha256(json.dumps(intent,sort_keys=True).encode()).hexdigest(),
                before={'revision':revision,'receipt_cost_minor':receipt_cost_minor},after=after))
            return after
