"""Versioned scrypt hashes; no plaintext persistence or password logging."""
import hashlib
import hmac
import secrets
import re


def username(value: str) -> str:
    value = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,79}", value):
        raise ValueError("Username must contain 1–80 ASCII letters, digits, dot, dash or underscore")
    return value


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")
    salt = secrets.token_hex(16)
    return "scrypt-v1$" + salt + "$" + _derive(password, salt)


def _derive(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=32768,
                          r=8, p=3, maxmem=64 * 1024 * 1024, dklen=32).hex()


def verify_password(password: str, encoded: str | None) -> bool:
    # Unknown users incur the same expensive derivation as known users.
    try:
        version, salt, expected = (encoded or ("scrypt-v1$" + "00" * 16 + "$" + "00" * 32)).split("$")
        if version != "scrypt-v1" or len(salt) != 32 or len(expected) != 64:
            return False
        actual = _derive(password, salt)
        return hmac.compare_digest(actual, expected) and encoded is not None
    except (ValueError, TypeError):
        return False
