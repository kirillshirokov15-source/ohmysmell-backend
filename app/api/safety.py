import logging
from time import perf_counter
from starlette.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from app.services.order_lifecycle import InvalidOrderTransitionError
from app.logging_utils import log_event

logger = logging.getLogger(__name__)


def install_error_handlers(app):
    @app.exception_handler(InvalidOrderTransitionError)
    async def conflict(request, error):
        return JSONResponse(status_code=409, content={"detail": {"code": "state_conflict",
            "message": "Состояние изменилось. Обновите карточку и повторите действие."}})

    @app.exception_handler(RequestValidationError)
    async def validation(request, error):
        return JSONResponse(status_code=422, content={"detail": {"code": "validation_error",
            "fields": [{"loc": list(e["loc"]), "type": e["type"]} for e in error.errors()]}})

    @app.exception_handler(Exception)
    async def unexpected(request, error):
        log_event(logger, "api_failure", level=logging.ERROR, result=type(error).__name__)
        return JSONResponse(status_code=503, content={"detail": {"code": "service_unavailable",
            "message": "Сервис временно недоступен. Повторите запрос с тем же Idempotency-Key."}})


class RequestSafetyMiddleware:
    def __init__(self, app, max_body=262144):
        self.app, self.max_body = app, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = perf_counter()
        if scope.get("path") == "/integrations/tilda/orders":
            # Mutate the original server scope before Uvicorn formats access logs.
            # Proxy/edge logs need independent query suppression before enabling this mode.
            from urllib.parse import parse_qs
            query = scope.get("query_string", b"")
            scope["query_string"] = b""
            try:
                values = parse_qs(query.decode("ascii"), max_num_fields=10).get("secret", [])
                scope["tilda_query_secret"] = values[0] if len(values) == 1 else ""
            except (ValueError, UnicodeError):
                scope["tilda_query_secret"] = ""
        # Only the bounded Excel endpoint accepts a larger binary request body.
        limit = 2 * 1024 * 1024 if scope.get("path", "").startswith("/buying/suppliers/") and scope.get("path", "").endswith("/price-lists/preview") else self.max_body
        chunks, size = [], 0
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            size += len(event.get("body", b""))
            if size > limit:
                return await JSONResponse(status_code=413, content={"detail": {"code": "body_too_large"}})(scope, receive, send)
            chunks.append(event.get("body", b""))
            if not event.get("more_body", False):
                break
        delivered = False
        async def body():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
        response_started = False
        async def safe_send(event):
            nonlocal response_started
            if event["type"] == "http.response.start":
                response_started = True
            await send(event)
        try:
            await self.app(scope, body, safe_send)
        except Exception as error:
            # Consume unexpected exceptions before Uvicorn can log DB parameters,
            # raw HTTP provider responses or credential-bearing exception strings.
            log_event(logger, "api_failure", level=logging.ERROR, result=type(error).__name__)
            if not response_started:
                await JSONResponse(status_code=503, content={"detail": {"code": "service_unavailable",
                    "message": "Сервис временно недоступен. Повторите запрос с тем же Idempotency-Key."}})(scope, receive, send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            logger.info("api_request_completed route=%s duration_ms=%.2f", route, (perf_counter() - started) * 1000)
