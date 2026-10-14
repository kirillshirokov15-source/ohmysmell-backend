"""Standalone staging-only, read-only Hatuna counterparty API verification.

This intentionally runs a static HTTP server, NOT the Telegram poller. It has
no method or code path that can create MoySklad or CRM documents.
"""
import asyncio
import os
from http.server import HTTPServer, SimpleHTTPRequestHandler

from app.services.hatuna_accounting import verify_hatuna_counterparty


async def _check():
    return await verify_hatuna_counterparty(os.getenv("OMS_HATUNA_COUNTERPARTY_ID", ""))


def main():
    if os.getenv("APP_ENV") != "staging":
        raise RuntimeError("Hatuna read-only smoke is staging-only")
    print("HATUNA_READONLY_CHECK_STARTED", flush=True)
    try:
        asyncio.run(_check())
    except Exception as exc:
        # No tokens, names, emails, URLs or other personal/provider data.
        print(f"HATUNA_READONLY_CHECK_FAILED:{type(exc).__name__}", flush=True)
    else:
        print("HATUNA_READONLY_CHECK_VERIFIED", flush=True)

    # Railway's staging manager service is intentionally a harmless HTTP
    # smoke container until a separate safe acceptance is completed.
    HTTPServer(("0.0.0.0", 8080), SimpleHTTPRequestHandler).serve_forever()


if __name__ == "__main__":
    main()
