"""Standalone worker. A database session lock owns the sole poller per bot ID.
Health is live while a replacement waits for the previous deployment to drain.
"""
import asyncio
import contextlib
import hashlib
import logging
import os
from aiohttp import web
from aiogram import Bot
from aiogram.exceptions import TelegramConflictError
from aiogram.methods import GetUpdates
from sqlalchemy import text
from app.config.settings import settings
from app.logging_utils import configure_application_logging, log_event

logger = logging.getLogger(__name__)


def validate_worker(role):
    if role not in {"manager", "client"}:
        raise ValueError("Unknown worker role")
    if settings.environment == "production" and os.getenv("BOT_PRODUCTION_ACTIVATED", "false").lower() != "true":
        raise ValueError("Production worker requires explicit activation")
    settings.validate_runtime()
    if settings.external_writes_enabled:
        raise ValueError("Worker requires external writes disabled")
    key = "TELEGRAM_BOT_TOKEN" if role == "manager" else "CLIENT_TELEGRAM_BOT_TOKEN"
    token = os.getenv(key, "")
    if not token:
        raise ValueError(key + " is required")
    if role == "client" and token == os.getenv("TELEGRAM_BOT_TOKEN"):
        raise ValueError("Client and manager require different bot tokens")
    if not settings.database_url:
        raise ValueError("Database is required")
    return token


class OwnedBot(Bot):
    def __init__(self, token, failed):
        super().__init__(token=token)
        self.failed = failed

    async def __call__(self, method, request_timeout=None):
        try:
            return await super().__call__(method, request_timeout=request_timeout)
        except TelegramConflictError:
            # An unmanaged poller may use another DB: fail closed, never fight it.
            self.failed.set()
            raise


async def run_worker(role="manager", *, drop_pending_updates=False):
    token = validate_worker(role)
    configure_application_logging()
    logging.getLogger("aiogram").setLevel(logging.CRITICAL)
    from app.database.session import engine
    failure = asyncio.Event()
    bot = OwnedBot(token, failure)
    if role == "manager":
        from app.bot.telegram_bot import dp
    else:
        from app.bot.client_bot import create_dispatcher
        dp = create_dispatcher()
    state = {"status": "starting", "role": role, "environment": settings.environment, "polling": False, "external_writes": False}
    async def health(_):
        return web.json_response(state, status=503 if failure.is_set() else 200)
    server = web.Application()
    server.router.add_get("/health", health)
    runner = web.AppRunner(server)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8081"))).start()
    lock_key = int.from_bytes(hashlib.sha256(f"telegram-poller:{bot.id}".encode()).digest()[:8], "big", signed=True)
    tasks = []
    try:
        async with engine.connect() as connection:
            state["status"] = "waiting_for_poller_lock"
            log_event(logger, "worker_starting", role=role, environment=settings.environment, external_writes=False)
            while not await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}):
                await asyncio.sleep(2)
            await connection.commit()
            # Never remove somebody else's webhook silently.
            info = await bot.get_webhook_info()
            if info.url:
                raise RuntimeError("Webhook exists; polling refused")
            if drop_pending_updates:
                await bot.delete_webhook(drop_pending_updates=True)
            async def watchdog():
                while not failure.is_set():
                    await asyncio.sleep(2)
                    try:
                        await asyncio.wait_for(connection.scalar(text("SELECT 1")), timeout=5)
                        await connection.commit()
                    except Exception:
                        failure.set()
                log_event(logger, "worker_ownership_lost", role=role, result="stopping")
            await bot.get_me()
            state.update(status="ready", polling=True)
            log_event(logger, "worker_ready", role=role, external_writes=False, polling_instances=1)
            polling = asyncio.create_task(dp.start_polling(bot, handle_as_tasks=True, tasks_concurrency_limit=8, close_bot_session=False))
            watch = asyncio.create_task(watchdog())
            tasks = [polling, watch]
            if role == "manager":
                from app.workers.notifications import main as notifications
                tasks.append(asyncio.create_task(notifications()))
            done, _ = await asyncio.wait(tasks[:2], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if not polling.done():
                await dp.stop_polling()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
    finally:
        state.update(status="stopped", polling=False)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await bot.session.close()
        await runner.cleanup()
        await engine.dispose()
        log_event(logger, "worker_stopped", role=role)
    if failure.is_set():
        raise RuntimeError("Polling ownership lost")


def main():
    try:
        asyncio.run(run_worker(os.getenv("BOT_ROLE", "manager")))
    except Exception as error:
        configure_application_logging()
        log_event(logger, "worker_startup_failed", result=type(error).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
