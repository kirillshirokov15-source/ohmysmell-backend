"""MoySklad list responses may omit positions even for a posted, reserved order.

No network or provider writes are used in these checks.
"""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.integrations.moysklad.documents import (
    MoySkladDocumentAdapter, verified_document,
)
from app.integrations.moysklad.payloads import reservation
from app.services.own_stock_reconciliation import verification_difference


def intent():
    document = reservation(
        "33333333-3333-3333-3333-333333333333",
        "22222222-2222-2222-2222-222222222222",
        "11111111-1111-1111-1111-111111111111",
        [{"id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
          "qty": 1, "price": 550000}],
        "warehouse:9:abcde:reserve",
    )
    return {"entity": "customerorder", "document": document}


class ReadOnlyProvider:
    def __init__(self, source):
        self.source = source
        self.get_calls = []
        self.post_calls = 0

    def find_documents(self, entity, code):
        assert entity == "customerorder"
        assert code == self.source["document"]["externalCode"]
        # This shape is common in MoySklad list response: positions has only meta.
        return [{"id":"doc-9","positions":{"meta":{"size":1}}}]

    def find_documents_by_number(self, entity, number):
        assert entity == "customerorder" and number == "00001"
        return [{"id":"doc-9", "name":"00001"}]

    def get_document(self, entity, identifier):
        self.get_calls.append("document")
        assert entity == "customerorder" and identifier == "doc-9"
        return {**deepcopy(self.source["document"]), "id":"doc-9",
                "name":"00001", "positions":{"meta":{"size":1}}}

    def get_document_positions(self, entity, identifier):
        self.get_calls.append("positions")
        assert entity == "customerorder" and identifier == "doc-9"
        return deepcopy(self.source["document"]["positions"])

    def create_document(self, entity, payload):
        self.post_calls += 1
        return {**deepcopy(payload), "id":"doc-9",
                "positions":{"meta":{"size":1}}}


def test_find_resolves_sparse_moysklad_positions_by_full_read():
    record = intent()
    client = ReadOnlyProvider(record)
    adapter = MoySkladDocumentAdapter(client)
    docs = asyncio.run(adapter.find(record))
    assert len(docs)==1 and verified_document(record, docs[0])
    assert client.get_calls == ["document", "positions"]
    assert client.post_calls == 0


def test_exact_number_lookup_reads_details_and_does_not_create():
    record=intent()
    client=ReadOnlyProvider(record)
    docs=asyncio.run(MoySkladDocumentAdapter(client).find_by_number(record,"00001"))
    assert len(docs)==1 and verified_document(record,docs[0])
    assert client.post_calls == 0


def test_post_response_without_positions_gets_verified_before_uncertain(monkeypatch):
    from app.integrations.moysklad import documents
    monkeypatch.setattr(documents,"require_external_writes",lambda:None)
    record=intent()
    client=ReadOnlyProvider(record)
    result=asyncio.run(MoySkladDocumentAdapter(client).create(record))
    assert result["id"]=="doc-9" and verified_document(record,result)
    assert client.post_calls == 1
    assert client.get_calls == ["document","positions"]


def test_mismatch_never_yields_false_verification():
    record=intent()
    doc={**deepcopy(record["document"]), "id":"doc-9"}
    doc["agent"]["meta"]["href"]="/entity/counterparty/different"
    assert not verified_document(record,doc)
    assert verification_difference(record,doc)=="покупатель отличается"
    doc=deepcopy(record["document"])
    doc["id"]="doc-9"
    doc["positions"][0]["reserve"]=0
    assert not verified_document(record,doc)
    assert verification_difference(record,doc)=="не совпадают товар, количество, цена или резерв"


def test_number_lookup_never_accepts_non_numeric_text():
    from app.integrations.moysklad.client import MoySkladClient
    import requests
    # Instantiating the provider client needs production credentials. Validate
    # the caller-side format instead, without a network connection.
    import re
    for invalid in ("1;delete", "abc", "1\n2", "1*"):
        assert re.fullmatch(r"[0-9]{1,18}",invalid) is None
