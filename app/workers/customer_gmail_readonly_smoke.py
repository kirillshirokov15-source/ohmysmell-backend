"""Production customer Gmail OAuth smoke test. Read-only; zero CRM or supplier writes.

This process is deliberately *not* the real customer-email poller. It validates
exact Gmail readonly credentials and one inbox metadata listing. No message
body, sender, subject, ID or email address enters logs or health responses.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def verify_once():
    from app.integrations.email.secret_files import prepare_secret_files
    from app.integrations.email.gmail_provider import GmailEmailProvider
    from app.config.settings import settings
    if settings.environment != "production":
        raise RuntimeError("This OAuth smoke service is production-only")
    if settings.external_writes_enabled:
        raise RuntimeError("External MoySklad writes must be disabled")
    if os.getenv("EMAIL_PRODUCTION_ACTIVATED", "false").lower() == "true":
        raise RuntimeError("Production ingestion cannot run in read-only smoke")
    prepare_secret_files("customer")
    # Makes only GET profile and list calls through the explicitly GET-only transport.
    result = GmailEmailProvider().readonly_smoke_check(max_messages=1)
    return bool(result.get("account"))


def main():
    try:
        verified = verify_once()
    except Exception as error:
        verified = False
        # Only the exception class, never tokens/account/message metadata.
        print("CUSTOMER_GMAIL_READONLY_SMOKE_FAILED:" + type(error).__name__, flush=True)
    else:
        print("CUSTOMER_GMAIL_READONLY_SMOKE_VERIFIED", flush=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/health":
                self.send_response(404)
                self.end_headers()
                return
            payload = json.dumps({"status": "verified" if verified else "verification_failed"}).encode()
            self.send_response(200 if verified else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt, *args):
            pass

    port = int(os.getenv("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
