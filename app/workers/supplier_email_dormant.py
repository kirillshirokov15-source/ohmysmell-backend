"""Safe production placeholder for supplier Gmail (Buying) before OAuth enrollment.

Launch this instead of supplier_email_runtime until BOTH customer and supplier
mailboxes are separately bound and the manager enables sending/replies.
It NEVER contacts Gmail, sends email or mutates external systems.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        payload = json.dumps({
            "status": "ready_for_oauth",
            "supplier_mail_connected": False,
            "supplier_sending_active": False,
            "supplier_reply_polling_active": False,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt, *args):
        pass


def main():
    if os.getenv("SUPPLIER_EMAIL_SEND_ENABLED", "false").casefold() == "true":
        raise RuntimeError("Cannot run dormant supplier worker with sending enabled")
    if os.getenv("EXTERNAL_WRITES_ENABLED", "false").casefold() == "true":
        raise RuntimeError("Cannot run dormant supplier worker with external writes enabled")
    print("SUPPLIER_GMAIL_DORMANT: OAuth not connected; zero Gmail traffic", flush=True)
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()


if __name__ == "__main__":
    main()
