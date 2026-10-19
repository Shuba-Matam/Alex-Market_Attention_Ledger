"""Email+password, Google, and demo authentication.

Passwords are stored as scrypt hashes (per-user random salt, stdlib hashlib).
Session tokens are HMAC-signed `user_id.expiry.signature` strings (stdlib hmac)
with a 30-day lifetime. The subject is the immutable users.id rather than a
username, so renaming an account cannot re-point an existing session at a
different ledger.

Google Sign-In is the one place stdlib is not enough: ID tokens are RS256 JWTs
that must be verified against Google's rotating JWKS, so `google-auth` is used
for that single call. Everything else here remains dependency-free.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

from .db import DATABASE_PATH

TOKEN_LIFETIME_SECONDS = 30 * 24 * 3600
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD_LENGTH = 8
RESET_CODE_TTL_SECONDS = 10 * 60
RESET_MAX_ATTEMPTS = 5
_GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Sign-in throttling: at most this many failures per key inside the window.
# In-process state fits a single-worker deployment; it is intentionally not
# persisted, so a restart clears the counters and locks nobody out.
_LOGIN_WINDOW_SECONDS = 15 * 60
_LOGIN_MAX_FAILURES = 8
_failure_lock = threading.Lock()
_failures: dict[str, list[float]] = {}


def google_client_id() -> Optional[str]:
    """Configured Google OAuth client ID, or None when Google login is disabled."""
    return os.environ.get("GOOGLE_CLIENT_ID") or None


def _secret() -> bytes:
    """Signing secret from AUTH_SECRET, else a generated key persisted next to
    the database (resolved lazily so tests can repoint DATABASE_PATH first).
    On ephemeral deploys the key regenerates, which simply expires all sessions."""
    global _SECRET
    if _SECRET is not None:
        return _SECRET
    env = os.environ.get("AUTH_SECRET")
    if env:
        _SECRET = env.encode()
        return _SECRET
    key_file = Path(DATABASE_PATH).parent / "auth_secret.key"
    try:
        if key_file.exists():
            _SECRET = key_file.read_bytes().strip()
        else:
            _SECRET = secrets.token_hex(32).encode()
            key_file.write_bytes(_SECRET)
    except OSError:
        _SECRET = secrets.token_hex(32).encode()
    return _SECRET


_SECRET = None  # type: ignore[assignment]


def valid_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email or ""))


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt,
                            n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: Optional[str]) -> bool:
    if not stored or not stored.startswith("scrypt$"):
        return False
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex),
                                n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def make_token(user_id: int, token_version: int = 0, now: Optional[int] = None) -> str:
    expiry = int(now if now is not None else time.time()) + TOKEN_LIFETIME_SECONDS
    payload = f"{user_id}.{int(token_version)}.{expiry}"
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def verify_token(token: Optional[str], now: Optional[int] = None) -> Optional[tuple[int, int]]:
    """Return (user_id, token_version) for a valid, unexpired token; None otherwise."""
    if not token:
        return None
    parts = token.split(".")
    if len(parts) != 4:
        return None
    subject, version_raw, expiry_raw, signature = parts
    payload = f"{subject}.{version_raw}.{expiry_raw}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        user_id, token_version, expiry = int(subject), int(version_raw), int(expiry_raw)
    except ValueError:
        return None
    if expiry < int(now if now is not None else time.time()):
        return None
    return user_id, token_version


def generate_reset_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_reset_code(code: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", code.encode(), salt, 120_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_reset_code(code: str, stored: Optional[str]) -> bool:
    if not stored:
        return False
    try:
        scheme, salt_hex, digest_hex = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", code.encode(), bytes.fromhex(salt_hex), 120_000)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


def bearer_token(authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.startswith("Bearer "):
        return authorization[len("Bearer "):].strip()
    return None


def username_from_email(email: str) -> str:
    """Derive a display username from the email's local part."""
    local = (email.split("@", 1)[0] or "user").lower()
    cleaned = "".join(c for c in local if c.isalnum() or c in ".-&_")[:15] or "user"
    return cleaned


def verify_google_id_token(credential: str) -> dict:
    """Verify a Google ID token and return {'sub', 'email', 'name'}.

    Raises ValueError when the token is invalid, the audience is not our client
    ID, the issuer is not Google, or the email is unverified. Verification is
    delegated to google-auth because the signature is RS256 over Google's
    rotating public keys.
    """
    client_id = google_client_id()
    if not client_id:
        raise ValueError("Google sign-in is not configured on this server")
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token as google_id_token
    except ImportError as error:  # pragma: no cover - dependency is declared
        raise ValueError("Google sign-in dependencies are unavailable") from error
    try:
        # Checks signature, exp, and audience; raises ValueError otherwise.
        claims = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id)
    except Exception as error:
        raise ValueError("Google sign-in failed") from error
    if claims.get("iss") not in _GOOGLE_ISSUERS:
        raise ValueError("Google sign-in failed")
    # An unverified address must never link to an account: anyone could claim it.
    if not claims.get("email") or not claims.get("email_verified"):
        raise ValueError("your Google account has no verified email address")
    return {"sub": str(claims["sub"]), "email": str(claims["email"]).strip().lower(),
            "name": str(claims.get("name") or "")}


def _recent_failures(key: str, now: float) -> list[float]:
    cutoff = now - _LOGIN_WINDOW_SECONDS
    return [stamp for stamp in _failures.get(key, []) if stamp > cutoff]


def rate_limit_wait(key: str, now: Optional[float] = None) -> float:
    """Seconds to wait before this key may try again; 0 when allowed."""
    current = time.time() if now is None else now
    with _failure_lock:
        bucket = _recent_failures(key, current)
        if bucket:
            _failures[key] = bucket
        else:
            _failures.pop(key, None)
        if len(bucket) < _LOGIN_MAX_FAILURES:
            return 0.0
        return max(0.0, bucket[0] + _LOGIN_WINDOW_SECONDS - current)


def record_auth_failure(key: str, now: Optional[float] = None) -> None:
    current = time.time() if now is None else now
    with _failure_lock:
        bucket = _recent_failures(key, current)
        bucket.append(current)
        _failures[key] = bucket


def clear_auth_failures(key: str) -> None:
    with _failure_lock:
        _failures.pop(key, None)


def reset_auth_failures() -> None:
    """Test hook: forget every recorded failure."""
    with _failure_lock:
        _failures.clear()
