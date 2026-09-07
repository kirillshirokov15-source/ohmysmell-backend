import asyncio
import json


async def request(app, method, path, payload=None, headers=None):
    messages = []
    delivered = False
    body = json.dumps(payload).encode() if payload is not None else b""
    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        await asyncio.Future()
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path, "raw_path": path.encode(),
        "query_string": b"", "root_path": "", "server": ("test", 80), "client": ("127.0.0.1", 1),
        "headers": [(b"content-type", b"application/json")] +
                   [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    try:
        await app(scope, receive, send)
    except Exception:
        if not messages:
            raise
    start = next(m for m in messages if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], json.loads(data), dict(start["headers"])
