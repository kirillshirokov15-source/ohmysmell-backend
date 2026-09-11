import json
import logging
import sys


def configure_application_logging() -> None:
    """Expose application metrics without enabling verbose dependency logs."""
    logger = logging.getLogger("app")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    safe_fields = {key: value for key, value in fields.items() if value is not None}
    logger.info(json.dumps({"event": event, **safe_fields}, ensure_ascii=False))
