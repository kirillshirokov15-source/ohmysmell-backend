"""No-network assembly read parallelism regression.

Checks the fresh stock guard, a truly concurrent read, flag rollback and
syntax of the manager callback. Does not create an order or touch MoySklad.
"""
import asyncio
import ast
import os
from pathlib import Path
from app.services.product_service import ProductService


class Gateway:
    def __init__(self, parallel):
        self.parallel = parallel
        self.starts = []
        self.fresh_args = []

    async def query(self, name):
        self.starts.append(name)
        # Concurrent calls must all start before any completes.
        await asyncio.sleep(0)
        if self.parallel:
            assert set(self.starts) == {"products", "stores", "stock"}, self.starts
        return {"products": [{"id": "sample"}], "stores": [{"id": "main"}],
                "stock": {"rows": []}}[name]

    async def get_products(self):
        return await self.query("products")

    async def get_stores(self, *, fresh=False):
        self.fresh_args.append(("stores", fresh))
        return await self.query("stores")

    async def get_stock_by_store(self, *, fresh=False):
        self.fresh_args.append(("stock", fresh))
        return await self.query("stock")


async def verify():
    service = ProductService(client=object())
    service._build_catalog = lambda products, stores, report, *_: [
        {"products": products, "stores": stores, "stock": report}]
    os.environ["OMS_MANAGER_ASSEMBLY_FASTPATH_ENABLED"] = "true"
    gateway = Gateway(parallel=True)
    service.async_gateway = gateway
    results = await service.get_catalog_async(fresh_stock=True, include_supply=False)
    assert results[0]["products"] == [{"id": "sample"}]
    assert gateway.fresh_args == [("stores", True), ("stock", True)]
    assert set(gateway.starts) == {"products", "stores", "stock"}
    # Turning the flag off fully restores the previous sequential route.
    os.environ["OMS_MANAGER_ASSEMBLY_FASTPATH_ENABLED"] = "false"
    gateway = Gateway(parallel=False)
    service.async_gateway = gateway
    await service.get_catalog_async(fresh_stock=True, include_supply=False)
    assert gateway.starts == ["products", "stores", "stock"]
    assert gateway.fresh_args == [("stores", True), ("stock", True)]
    # Ordinary catalog views must also retain their original request order.
    os.environ["OMS_MANAGER_ASSEMBLY_FASTPATH_ENABLED"] = "true"
    gateway = Gateway(parallel=False)
    service.async_gateway = gateway
    await service.get_catalog_async(fresh_stock=False, include_supply=False)
    assert gateway.starts == ["products", "stores", "stock"]
    assert gateway.fresh_args == [("stores", False), ("stock", False)]


def main():
    files = ("app/services/product_service.py", "app/bot/telegram_bot.py")
    for filename in files:
        ast.parse(Path(filename).read_text(encoding="utf-8-sig"), filename=filename)
    bot = Path("app/bot/telegram_bot.py").read_text(encoding="utf-8-sig")
    assert 'phase="status_commit"' in bot
    assert 'phase="reservation_and_validation"' in bot
    assert 'phase="render"' in bot
    assert 'phase="telegram_publish"' in bot
    assert "assembled_prefetched_plans = await asyncio.gather" in bot
    asyncio.run(verify())
    print("MANAGER_ASSEMBLY_PARALLEL_READS_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
