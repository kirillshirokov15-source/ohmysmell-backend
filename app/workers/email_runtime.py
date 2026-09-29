"""One mailbox per process, with independent ownership, health and retry state."""
import asyncio
import hashlib
import logging
import os
from pathlib import Path
from time import monotonic, perf_counter
from aiohttp import web
from sqlalchemy import text
from app.config.settings import settings
from app.logging_utils import configure_application_logging, log_event
from app.workers.lifecycle import install_stop_signals

logger = logging.getLogger("app.workers.email_runtime")


def validate_email():
    settings.validate_runtime("email")
    if settings.environment == "production" and os.getenv("EMAIL_PRODUCTION_ACTIVATED", "false").lower() != "true":
        raise ValueError("Production email worker requires explicit activation")
    if not settings.gmail_token_file or not Path(settings.gmail_token_file).is_file():
        from app.integrations.email.health import MailboxNotConfigured
        raise MailboxNotConfigured("CUSTOMER_GMAIL_TOKEN_FILE must contain an authorized readonly token")
    if settings.email_poll_interval < 5:
        raise ValueError("EMAIL_POLL_INTERVAL must be at least 5 seconds")
    ids = os.getenv("GMAIL_ALLOWED_MESSAGE_IDS", "").strip()
    query = os.getenv("GMAIL_ORDER_QUERY", "").strip()
    query_activated = os.getenv("GMAIL_QUERY_INTAKE_ENABLED", "false").lower() == "true"
    if settings.environment in {"staging", "production"} and not ids and not (query and query_activated):
        raise ValueError("Gmail requires explicit allowed message IDs or an activated order query")


async def run(worker=None, *, mailbox_role='customer'):
    if mailbox_role not in ('customer', 'supplier'):
        raise ValueError('Unknown Gmail worker role')
    from app.integrations.email.secret_files import prepare_secret_files
    # Hard environment/write boundaries always fail closed before opening a service.
    settings.validate_runtime('email')
    if settings.environment == 'production' and os.getenv('EMAIL_PRODUCTION_ACTIVATED', 'false').lower() != 'true':
        raise ValueError('Production email worker requires explicit activation')
    configuration_error = None
    try:
        if mailbox_role == 'customer':
            prepare_secret_files()
            validate_email()
        else:
            prepare_secret_files('supplier')
            from app.integrations.email.mailboxes import configured
            if not configured('supplier'):
                from app.integrations.email.health import MailboxNotConfigured
                raise MailboxNotConfigured()
            if settings.email_poll_interval < 5:
                raise ValueError('EMAIL_POLL_INTERVAL must be at least 5 seconds')
    except (ValueError, RuntimeError, OSError) as error:
        configuration_error = error
    configure_application_logging()
    from app.monitoring import configure_monitoring
    configure_monitoring()
    from app.database.session import engine
    from app.workers.email_ingestion import EmailIngestionWorker
    from app.integrations.email.health import classify, retry_delay
    from app.services.integration_health import record_email
    if mailbox_role == 'supplier':
        from app.services.integration_health import record_mailbox
        async def record_email(status, delay):
            await record_mailbox('supplier', status, delay)
    stop, failed = asyncio.Event(), asyncio.Event()
    restore_signals = install_stop_signals(asyncio.get_running_loop(), stop)
    state = {"status": "starting", "role": mailbox_role + "-email", "environment": settings.environment, "ready": False}
    last_success = None
    async def live(_):
        return web.json_response(state, status=503 if failed.is_set() else 200)
    async def ready(_):
        fresh = last_success is not None and monotonic() - last_success < max(180, settings.email_poll_interval * 3)
        return web.json_response(state, status=200 if state["ready"] and fresh and not failed.is_set() else 503)
    server = web.Application()
    server.router.add_get("/health", live)
    server.router.add_get("/ready", ready)
    runner = web.AppRunner(server)
    tasks = []
    try:
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8082"))).start()
        # Each mailbox owns a different DB lock; replicas of that mailbox share it.
        # Preserve the existing CUSTOMER ownership key during rolling upgrades.
        namespace = b'gmail-supplier-replies:v1' if mailbox_role == 'supplier' else b'gmail-ingestion:default-account'
        key = int.from_bytes(hashlib.sha256(namespace).digest()[:8], "big", signed=True)
        async with engine.connect() as conn:
            log_event(logger, "worker_starting", role=mailbox_role + '-email', environment=settings.environment)
            state["status"] = "waiting_for_worker_lock"
            while not await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}):
                if stop.is_set():
                    return
                await asyncio.sleep(2)
            await conn.commit()
            async def poll():
                nonlocal last_success, worker
                failures, previous_error = 0, None
                while not stop.is_set():
                    started = perf_counter()
                    delay = settings.email_poll_interval
                    try:
                        if configuration_error:
                            raise configuration_error
                        if worker is None:
                            if mailbox_role == 'customer':
                                from app.integrations.email.gmail_provider import GmailEmailProvider
                                provider = GmailEmailProvider()
                                provider.service = await asyncio.to_thread(provider._build_service)
                                cursor_key = await asyncio.to_thread(provider.cursor_key)
                                worker = EmailIngestionWorker(provider=provider, provider_name=cursor_key)
                            else:
                                from app.workers.supplier_email_runtime import SupplierEmailWorker
                                worker = await SupplierEmailWorker.create()
                        stats = await worker.run_once()
                        if stats["failed"]:
                            raise RuntimeError("Ingestion batch incomplete")
                        last_success = monotonic()
                        state.update(status="ready", ready=True, action=None, retry_after_seconds=delay)
                        await record_email('connected', delay)
                        failures, previous_error = 0, None
                        log_event(logger, "email_poll_completed", duration_ms=round((perf_counter()-started)*1000, 2), result="success", **stats)
                    except Exception as error:
                        failures += 1
                        category = classify(error)
                        delay = retry_delay(category, failures, settings.email_poll_interval)
                        state.update(status=category, ready=False, retry_after_seconds=delay,
                            action=f'run_{mailbox_role}_gmail_oauth_then_update_worker_token' if category == 'reauth_required' else 'check_worker_configuration_or_connectivity')
                        try:
                            await record_email(category, delay)
                        except Exception:
                            state.update(status='database_unavailable', ready=False)
                        if previous_error != category:
                            log_event(logger, "email_poll_failed", level=logging.ERROR, result=category)
                        previous_error = category
                    try:
                        await asyncio.wait_for(stop.wait(), delay)
                    except TimeoutError:
                        pass
            async def watchdog():
                while not stop.is_set():
                    await asyncio.sleep(2)
                    try:
                        await asyncio.wait_for(conn.scalar(text("SELECT 1")), 5)
                        await conn.commit()
                    except Exception:
                        state.update(status='database_unavailable', ready=False)
                        failed.set()
                        stop.set()
                        return
            tasks = [asyncio.create_task(poll()), asyncio.create_task(watchdog()), asyncio.create_task(stop.wait())]
            log_event(logger, "worker_ownership_acquired", role=mailbox_role + '-email', polling_instances=1)
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            stop.set()
            # Finish the current atomic ingestion where possible. Cursor only advances
            # after all messages commit; a killed run replays deduplicated messages.
            try:
                await asyncio.wait_for(asyncio.shield(tasks[0]), 25)
            except TimeoutError:
                pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        state.update(status="stopped", ready=False)
        await runner.cleanup()
        await engine.dispose()
        restore_signals()
        log_event(logger, "worker_stopped", role=mailbox_role + '-email')
    if failed.is_set():
        raise RuntimeError("Email ownership lost")


def main(mailbox_role='customer'):
    try:
        asyncio.run(run(mailbox_role=mailbox_role))
    except Exception as error:
        configure_application_logging()
        log_event(logger, "worker_startup_failed", level=logging.ERROR, role=mailbox_role + '-email', result=type(error).__name__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
