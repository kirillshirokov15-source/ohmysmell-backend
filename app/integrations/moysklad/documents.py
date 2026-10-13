"""Document transport and exact reconciliation shared by warehouse operations."""
import asyncio
from copy import deepcopy
from collections import Counter
from app.integrations.moysklad.client import MoySkladClient
from app.integrations.write_guard import require_external_writes


def verified_document(intent, document):
    expected = intent['document']
    if not document.get('id'):
        return False
    for key in ('externalCode', 'syncId', 'applicable'):
        if key in expected and expected[key] != document.get(key):
            return False
    for key in ('organization', 'agent', 'store', 'customerOrder', 'purchaseOrder'):
        if key in expected and expected[key]['meta']['href'] != document.get(key, {}).get('meta', {}).get('href'):
            return False
    positions = document.get('positions', [])
    if isinstance(positions, dict):
        if positions.get('meta', {}).get('size', len(positions.get('rows', []))) != len(positions.get('rows', [])):
            return False
        positions = positions.get('rows', [])
    def signature(row):
        # The provider may return a default 0 for omitted enter position
        # valuation. That default is NOT the website's retail sale price.
        price = row.get('price')
        if intent['entity'] == 'enter' and price is None:
            price = 0
        return (row.get('assortment', {}).get('meta', {}).get('href'), row.get('quantity'),
                price, row.get('reserve', 0))
    return Counter(map(signature, expected['positions'])) == Counter(map(signature, positions))


class MoySkladDocumentAdapter:
    def __init__(self, client=None):
        self.client = client

    async def create(self, intent):
        require_external_writes()
        client = self.client or MoySkladClient()
        result = (await asyncio.to_thread(client.update_document, intent['entity'], intent['update_id'], intent['document'])
                  if intent.get('update_id') else
                  await asyncio.to_thread(client.create_document, intent['entity'], intent['document']))
        # A malformed response is uncertain even if its HTTP status was 200.
        if not verified_document(intent, result):
            raise ValueError('Created document requires reconciliation')
        return result

    async def find(self, intent):
        client = self.client or MoySkladClient()
        return await asyncio.to_thread(client.find_documents, intent['entity'], intent['document']['externalCode'])


class FakeDocumentAdapter:
    """Explicit in-memory provider; cannot reach MoySklad or accept credentials."""
    def __init__(self):
        self.documents = {}
        self.calls = 0
        self.lose_response = False

    async def create(self, intent):
        key = intent['document']['externalCode']
        self.calls += 1
        result = {**deepcopy(intent['document']), 'id': intent.get('update_id') or 'fake-' + intent['document']['syncId'], 'simulated': True}
        self.documents[key] = result
        if self.lose_response:
            raise TimeoutError('Simulated response loss')
        return result

    async def find(self, intent):
        result = self.documents.get(intent['document']['externalCode'])
        return [deepcopy(result)] if result else []
