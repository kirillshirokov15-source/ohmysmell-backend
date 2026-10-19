"""Supplier Gmail attachments enter the existing Buying import/history workflow."""
import hashlib
from sqlalchemy import select, func
from app.models.supply import Supplier, ProductSupply
from app.models.buying import BuyingImport, BuyingProduct
from app.services import buying
from app.services.buying_excel import ParserConfig, parse_price_list, normalize


async def import_attachment(session, *, account, message_id, attachment_id, sender,
                            authenticated, filename, data, products):
    if not authenticated or not account or not message_id or not attachment_id:
        raise ValueError('Verified supplier message identity required')
    if '/' in filename or '\\' in filename or len(filename)>255:
        raise ValueError('Unsafe attachment name')
    key=hashlib.sha256(('supplier-price:'+account.casefold()+':'+message_id+':'+attachment_id).encode()).hexdigest()
    await buying.lock(session)
    existing=await session.scalar(select(BuyingImport).where(BuyingImport.source_key==key))
    if existing:
        return existing
    suppliers=(await session.scalars(select(Supplier).where(func.lower(Supplier.email)==sender.strip().casefold(),
        Supplier.status=='active', Supplier.supplier_type=='external_wholesaler'))).all()
    if len(suppliers)!=1:
        raise ValueError('Supplier sender missing or ambiguous')
    supplier, config=await buying.supplier_get(session,suppliers[0].id)
    rows=parse_price_list(data,filename,ParserConfig(**config.parser))
    record=await buying.preview_import(session,supplier.id,filename,rows)
    record.source_key=key
    # Exact SKU only. A missing/ambiguous SKU never silently binds by name.
    index={}
    for product in products:
        if product.get('archived'):
            continue
        for field in ('article','code'):
            value=str(product.get(field) or '').strip().casefold()
            if value:
                index.setdefault(value,set()).add(product['id'])
    for row in rows:
        if row.get('error') or row.get('offer_id'):
            continue
        candidates=sorted(index.get(str(row.get('sku') or '').strip().casefold(),set()))
        row['candidates']=candidates
        row['product_id']=candidates[0] if len(candidates)==1 else None
        row['state']='ambiguous' if len(candidates)>1 else 'new'
        if row['product_id']:
            pid=row['product_id']
            if not await session.get(ProductSupply,pid):
                session.add(ProductSupply(product_id=pid,source_type='external'))
                await session.flush()
            if not await session.get(BuyingProduct,pid):
                session.add(BuyingProduct(product_id=pid,name=row['name'],normalized_name=normalize(row['name'])))
                await session.flush()
    record.counts={**record.counts,'ambiguous':sum(r.get('state')=='ambiguous' for r in rows),
                   'new':sum(r.get('state')=='new' for r in rows)}
    record.rows=rows
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(record,'rows')
    await session.flush()
    if not record.counts['invalid'] and not record.counts['ambiguous']:
        await buying.confirm_import(session,supplier.id,record.id)
    return record
