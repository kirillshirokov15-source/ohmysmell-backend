"""Staging-only read-only preview. No submit, outbox send, finalize or apply mode."""
import argparse
import asyncio
import json
import os

from app.config.settings import settings
from app.services.order_desk import DeskService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-id", type=int, default=38)
    parser.add_argument("--actor", type=int, required=True, help="Assigned manager Telegram ID")
    parser.add_argument("--chat", type=int, required=True, help="Authorized manager group ID")
    args = parser.parse_args()
    if (settings.environment != "staging" or settings.external_writes_enabled
            or os.getenv("EXTERNAL_WRITES_ENABLED", "").lower() != "false"
            or os.getenv("TILDA_TEST_MODE", "").lower() != "true"
            or os.getenv("ORDER_DESK_SEND_ENABLED", "").lower() != "false"):
        parser.error("Requires staging, EXTERNAL_WRITES_ENABLED=false, TILDA_TEST_MODE=true, ORDER_DESK_SEND_ENABLED=false")
    result = asyncio.run(DeskService().reprice(args.draft_id, args.actor, args.chat, dry_run=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
