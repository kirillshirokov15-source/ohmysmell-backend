"""Explicit CLI for production/staging manager authorization.

Run only inside the appropriate Railway backend/manager service container
with the service DATABASE_URL. No bot command can grant its own access.
"""
import argparse
import asyncio
import os

from sqlalchemy import select
from app.database.session import async_session
from app.models.manager import Manager


def validate_cli(args, env):
    if args.telegram_id <= 0 or not (1 <= len(args.name.strip()) <= 255):
        raise ValueError("Positive Telegram ID and manager name are required")
    if not args.confirm:
        raise ValueError("Pass --confirm after verifying the person's identity")
    if env.get("APP_ENV") not in {"production", "staging"}:
        raise ValueError("Expected explicit APP_ENV production or staging")
    if env.get("APP_ENV") == "production" and not args.production:
        raise ValueError("Production requires --production")
    allowed = {int(x.strip()) for x in env.get("MANAGER_TELEGRAM_USER_IDS", "").split(",")
               if x.strip()}
    if args.telegram_id not in allowed:
        raise ValueError("First add this Telegram ID to Railway MANAGER_TELEGRAM_USER_IDS")
    return True


async def apply(args):
    validate_cli(args, os.environ)
    async with async_session() as session, session.begin():
        existing = await session.scalar(select(Manager).where(Manager.telegram_id == args.telegram_id))
        if existing:
            existing.name = args.name.strip()
            existing.is_active = not args.disable
        elif not args.disable:
            session.add(Manager(telegram_id=args.telegram_id, name=args.name.strip(), is_active=True))
        else:
            raise ValueError("Manager does not exist")
    print("Manager access updated; no group membership or role elevation changed.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-id", type=int, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument("--production", action="store_true")
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    asyncio.run(apply(args))


if __name__ == "__main__":
    main()
