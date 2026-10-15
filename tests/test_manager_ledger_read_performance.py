"""Read-only regression for batched operation status in manager navigation."""
import asyncio
from types import SimpleNamespace

from app.services.warehouse_documents import (
    plan_document_read_models, plan_key, frozen_plan,
)
from app.services.hatuna_accounting import hatuna_operation_keys


def plan(plan_id, *, x=False, count=4):
    allocation_type = "partner_x" if x else "own"
    allocations = [
        SimpleNamespace(id=plan_id * 100 + i, order_item_id=i, qty=1,
                        supply_status="awaiting_receipt" if x else "confirmed",
                        source_type=allocation_type)
        for i in range(count)
    ]
    return SimpleNamespace(id=plan_id, order_id=100, allocations=allocations)


class Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class FakeReadSession:
    def __init__(self, records):
        self.records = records
        self.calls = 0

    async def execute(self, query):
        self.calls += 1
        return Result(list(self.records.items()))


def test_many_allocations_use_one_query_and_empty_read_never_freezes():
    plans = [plan(1, x=True, count=10), plan(2, count=10), plan(3, count=10)]
    session = FakeReadSession({})
    state = asyncio.run(plan_document_read_models(session, plans))
    assert session.calls == 1
    assert len(state) == 3
    assert all(not item["frozen"] for item in state.values())
    assert not state[1]["reserved"] and not state[1]["shipped"]


def test_x_pending_receipt_freezes_even_if_external_result_unknown():
    x = plan(8, x=True)
    x_key = hatuna_operation_keys(100, 8)["receipt"]
    session = FakeReadSession({x_key: "pending"})
    state = asyncio.run(plan_document_read_models(session, [x]))
    assert state[x.id]["frozen"]
    assert session.calls == 1


def test_simulated_receipt_blocks_live_order_cancellation():
    x = plan(9, x=True)
    simulation_key = "simulation:" + hatuna_operation_keys(100, 9)["receipt"]
    assert asyncio.run(frozen_plan(FakeReadSession({simulation_key: "succeeded"}), x))


def test_reserve_then_successful_release_unfreezes_without_receipts():
    ordinary = plan(4)
    reserve = plan_key(ordinary, "reserve", False)
    release = plan_key(ordinary, "release", False)
    session = FakeReadSession({reserve: "succeeded"})
    assert asyncio.run(plan_document_read_models(session, [ordinary]))[4]["frozen"]
    session = FakeReadSession({reserve: "succeeded", release: "succeeded"})
    output = asyncio.run(plan_document_read_models(session, [ordinary]))
    assert output[4]["frozen"] is False
    assert output[4]["reserved"] is True


def test_real_ship_key_is_visible_even_if_not_confirmed():
    own = plan(5)
    ship = plan_key(own, "ship", False)
    session = FakeReadSession({ship: "pending"})
    output = asyncio.run(plan_document_read_models(session, [own]))
    assert output[5]["shipped"] is True


def test_zero_plans_avoids_unnecessary_query():
    session = FakeReadSession({})
    assert asyncio.run(plan_document_read_models(session, [])) == {}
    assert session.calls == 0
