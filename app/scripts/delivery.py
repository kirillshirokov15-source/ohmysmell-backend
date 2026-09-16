"""Trusted operator delivery command. Validation is offline; submit is guarded."""
import argparse
import asyncio
import json
from pathlib import Path
from app.config.settings import settings
from app.integrations.delivery import DeliveryDraft


async def execute(action, draft):
    from app.services.delivery_service import DeliveryService
    from app.database.session import engine
    settings.validate_runtime("manager")
    try:
        service = DeliveryService()
        if action == "prepare":
            record = await service.prepare(draft)
            return {"delivery_request_id": record.id, "status": record.status, "external_write": False}
        result = await service.submit(draft)
        return {"external_id": result["id"], "status": "submitted"}
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "prepare", "submit"])
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        if args.file.stat().st_size > 262144:
            raise ValueError("Payload too large")
        draft = DeliveryDraft.model_validate_json(args.file.read_text(encoding="utf-8-sig"))
        if args.action == "validate":
            print(json.dumps({"valid": True, "provider": draft.provider, "external_write": False}))
        else:
            print(json.dumps(asyncio.run(execute(args.action, draft))))
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
