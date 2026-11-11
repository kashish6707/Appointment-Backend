import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.config import get_settings

COOKIE_NAME = "clinically_session"

_hasher = PasswordHasher()  # argon2id with the library's recommended parameters
# Verified against when the account doesn't exist, so timing doesn't reveal that.
_DUMMY_HASH = _hasher.hash("not-a-real-password-0")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _cookie(value: str, max_age: int) -> str:
    # Built by hand so the attributes match the frontend contract exactly. No Domain, ever.
    cookie = f"{COOKIE_NAME}={value}; HttpOnly; SameSite=Lax; Path=/; Max-Age={max_age}"
    if get_settings().cookie_secure:
        cookie += "; Secure"
    return cookie


def session_cookie(token: str) -> str:
    return _cookie(token, get_settings().session_ttl_days * 24 * 60 * 60)


def cleared_cookie() -> str:
    return _cookie("", 0)
