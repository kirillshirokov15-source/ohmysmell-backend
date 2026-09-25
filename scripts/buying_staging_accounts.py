"""Windows-only staging bootstrap. Passwords are stored encrypted with user DPAPI."""
import asyncio
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import secrets
from dotenv import load_dotenv

ARTIFACT = Path('.staging-artifacts/buying-accounts.dpapi')


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('Use the interactive create_buying_user CLI on non-Windows systems')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, target = Blob(len(data), buffer), Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    operation = library.CryptUnprotectData if decrypt else library.CryptProtectData
    operation.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    operation.restype = wintypes.BOOL
    if not operation(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise RuntimeError('Windows credential encryption failed')
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(target.data)


def accounts():
    return json.loads(protect(ARTIFACT.read_bytes(), decrypt=True))


async def main():
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config, activate_staging_config
    activate_staging_config(validate_staging_config(dict(os.environ)))
    from app.database.session import async_session, engine
    from app.scripts.create_buying_user import save_user
    from app.services.buying_passwords import hash_password
    from app.models.buying import BuyingUser
    from sqlalchemy import select
    values = accounts() if ARTIFACT.exists() else {
        role: dict(username='buying_'+role, password=secrets.token_urlsafe(32)) for role in ('manager','picker')}
    # Persist encrypted recovery material before creating accounts. No password in argv/logs.
    ARTIFACT.parent.mkdir(exist_ok=True)
    if not ARTIFACT.exists():
        ARTIFACT.write_bytes(protect(json.dumps(values).encode()))
    try:
        for role, value in values.items():
            async with async_session() as session:
                existing = await session.scalar(select(BuyingUser).where(BuyingUser.username == value['username']))
            if existing:
                from app.services.buying_passwords import verify_password
                if not existing.is_active or existing.role != role or not verify_password(value['password'], existing.password_hash):
                    raise RuntimeError('Existing account differs; use the explicit password reset CLI')
            else:
                await save_user(async_session, value['username'], role, hash_password(value['password']))
            print('Staging account ready: '+value['username']+' ('+role+')')
    finally:
        await engine.dispose()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        print('Staging account bootstrap failed: '+type(error).__name__)
        raise SystemExit(1)
