import json
import logging


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    safe_fields = {key: value for key, value in fields.items() if value is not None}
    logger.info(json.dumps({"event": event, **safe_fields}, ensure_ascii=False))
