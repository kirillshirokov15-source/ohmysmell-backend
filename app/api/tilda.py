import hmac
import logging
import os
import re
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from app.api.auth import require_internal_api_token
from app.integrations.tilda import (
    TildaInvalid, TildaConfigurationError, normalize, parse_body,
    DEFAULT_FIELDS, field_map, path_value,
)
from app.services.order_desk import TildaIntake, DeskService, DeskError
from app.logging_utils import log_event

router = APIRouter(tags=["Tilda order desk"])
logger = logging.getLogger(__name__)


@router.post("/integrations/tilda/orders", status_code=202, responses={200: {"description": "Authenticated connection test or duplicate accepted"}})
async def receive_order(request: Request):
    log_event(logger, "tilda_webhook_received")
    secret = os.getenv("TILDA_WEBHOOK_SECRET", "")
    if len(secret) < 32:
        raise HTTPException(503, "tilda_not_configured")
    # Tilda API METHOD=Header, API NAME=X-Tilda-Secret, API KEY=the secret.
    # Starlette's header lookup is case-insensitive. Keep the existing query
    # fallback opt-in, and never use a body field as authentication.
    provided = request.headers.get("X-Tilda-Secret", "")
    if os.getenv("TILDA_ALLOW_QUERY_SECRET", "false").lower() == "true":
        provided = provided or request.scope.get("tilda_query_secret", "")
    if not provided or not hmac.compare_digest(provided.encode(), secret.encode()):
        log_event(logger, "tilda_webhook_rejected", reason="authentication")
        raise HTTPException(401, "invalid_tilda_secret")
    content_type = request.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in {"application/json", "application/x-www-form-urlencoded"}:
        raise HTTPException(415, "unsupported_content_type")
    try:
        data = parse_body(await request.body(), content_type)
        # Exact shape only: a real submission with an extra test field must
        # still pass the intake gate and normal order validation. No DB access.
        if data == {"test": "test"}:
            log_event(logger, "tilda_webhook_handshake_accepted")
            return PlainTextResponse("ok", status_code=200, headers={"Cache-Control": "no-store"})
        # Staging-only live-cart test. Non-test buyers remain handled by the
        # existing Tilda receivers; this endpoint does not persist their data.
        enabled = os.getenv("TILDA_ENABLED", "false").lower() == "true"
        test_mode = os.getenv("TILDA_TEST_MODE", "false").lower() == "true"
        if test_mode:
            if enabled or os.getenv("RAILWAY_ENVIRONMENT_NAME", "").lower() != "staging":
                raise HTTPException(503, "tilda_test_mode_invalid")
            allowed = os.getenv("TILDA_TEST_EMAIL", "").strip().casefold()
            if len(allowed) > 320 or not re.fullmatch(r"[^@\s]+@[^@\s]+", allowed):
                raise HTTPException(503, "tilda_test_email_not_configured")
            email_path = field_map("TILDA_FIELD_MAP_JSON", DEFAULT_FIELDS)["email"]
            incoming = path_value(data, email_path)
            if not isinstance(incoming, str) or incoming.strip().casefold() != allowed:
                log_event(logger, "tilda_test_order_ignored")  # Never log buyer PII.
                return PlainTextResponse("ignored", status_code=200,
                                         headers={"Cache-Control": "no-store"})
        elif not enabled:
            raise HTTPException(503, "tilda_not_configured")
        payload = normalize(data, request.headers.get("Idempotency-Key"))
        result = await TildaIntake().submit(payload)
    except TildaConfigurationError:
        log_event(logger, "tilda_webhook_rejected", reason="configuration")
        raise HTTPException(503, "tilda_configuration_invalid") from None
    except TildaInvalid as error:
        log_event(logger, "tilda_webhook_rejected", reason="validation")
        raise HTTPException(422, str(error)) from None
    except DeskError as error:
        raise HTTPException(409 if str(error) == "external_id_conflict" else 503, "tilda_unavailable_or_conflict") from None
    log_event(logger, "tilda_webhook_accepted", draft_id=result["draft_id"], duplicate=result["duplicate"])
    return JSONResponse(result, status_code=200 if result["duplicate"] else 202)


@router.post("/internal/tilda/drafts/{draft_id}/telegram-link", dependencies=[Depends(require_internal_api_token)])
async def issue_link(draft_id: int):
    """Trusted server only; caller must authenticate checkout ownership before disclosure."""
    if not 1 <= draft_id <= 2147483647:
        raise HTTPException(404, "not_found")
    try:
        result = await DeskService().issue_link(draft_id)
    except DeskError:
        raise HTTPException(409, "link_unavailable") from None
    return JSONResponse(result, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
