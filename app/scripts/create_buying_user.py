"""Create/update a local or staging Buying account using a hidden password prompt."""
import argparse
import asyncio
from getpass import getpass
from sqlalchemy import select, delete
from app.config.settings import settings
from app.models.buying import BuyingUser, BuyingSession
from app.services.buying_passwords import hash_password, username


async def save_user(factory, name, role, password_hash, replace=False):
    async with factory() as session, session.begin():
        user = await session.scalar(select(BuyingUser).where(BuyingUser.username == name).with_for_update())
        if user and not replace:
            raise ValueError("Account already exists; use --replace to reset and revoke all sessions")
        if user:
            await session.execute(delete(BuyingSession).where(BuyingSession.user_id == user.id))
            user.password_hash, user.role, user.is_active = password_hash, role, True
        else:
            session.add(BuyingUser(username=name, password_hash=password_hash, role=role))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--username', required=True, type=username)
    parser.add_argument('--role', required=True, choices=['manager', 'picker'])
    parser.add_argument('--staging', action='store_true', help='Use STAGING_DATABASE_URL; never DATABASE_URL')
    parser.add_argument('--replace', action='store_true')
    args = parser.parse_args()
    import os
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    url = settings.database_url
    if args.staging:
        from app.bot.staging_runner import validate_staging_config
        url = validate_staging_config(dict(os.environ)).database_url
    elif settings.environment != 'development':
        parser.error('Only local development or explicit --staging is supported')
    elif not url or __import__('urllib.parse', fromlist=['urlsplit']).urlsplit(url).hostname not in ('localhost', '127.0.0.1', '::1'):
        parser.error('Local mode requires a loopback database; use --staging for staging')
    password = getpass('Password (12+ characters): ')
    if password != getpass('Repeat password: '):
        parser.error('Passwords do not match')
    encoded = hash_password(password)
    del password
    async def run():
        from sqlalchemy.engine import make_url
        parsed = make_url(url)
        query = dict(parsed.query)
        sslmode = query.pop('sslmode', None)
        connect_args = {'timeout': 10, 'command_timeout': 30}
        if sslmode:
            connect_args['ssl'] = sslmode != 'disable'
        engine = create_async_engine(parsed.set(drivername='postgresql+asyncpg', query=query), connect_args=connect_args)
        try:
            await save_user(async_sessionmaker(engine, expire_on_commit=False), args.username, args.role, encoded, args.replace)
        finally:
            await engine.dispose()
    asyncio.run(run())
    print(f'Account ready: {args.username} ({args.role}); password not displayed')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError) as error:
        print('Account setup failed: ' + str(error))
        raise SystemExit(1)
    except Exception as error:
        print('Account setup failed: ' + type(error).__name__)
        raise SystemExit(1)
