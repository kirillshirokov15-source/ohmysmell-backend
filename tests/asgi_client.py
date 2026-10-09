import asyncio
import json
from urllib.parse import urlsplit


async def request(app, method, path, payload=None, headers=None, content=None, raise_errors=False):
    messages = []
    delivered = False
    body = content if content is not None else json.dumps(payload).encode() if payload is not None else b""
    url = urlsplit(path)
    path = url.path
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
        "query_string": url.query.encode(), "root_path": "", "server": ("test", 80), "client": ("127.0.0.1", 1),
        "headers": ([] if any(k.lower() == "content-type" for k in (headers or {})) else [(b"content-type", b"application/json")]) +
                   [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]}
    try:
        await app(scope, receive, send)
    except Exception:
        if raise_errors or not messages:
            raise
    start = next(m for m in messages if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        parsed = data.decode(errors='replace')
    return start["status"], parsed, dict(start["headers"])
