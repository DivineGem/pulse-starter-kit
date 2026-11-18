"""Operator session authentication and write-request intent checks."""
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import Cookie, Depends, Header, HTTPException, Response
from fastapi.responses import RedirectResponse
from .config import COOKIE_SECURE, OPERATOR_USERS, SESSION_TTL_SECONDS
from .database import connect, db_lock
from .utils import now_iso

SESSION_COOKIE = "pulse_operator_session"


def verify_credentials(username: str, password: str) -> str | None:
    """Validate supplied username/password without revealing which part was wrong."""
    # Use a dummy password for unknown users so response work is similar for both cases.
    expected_password = OPERATOR_USERS.get(username, "invalid-operator-password")
    user_ok = username in OPERATOR_USERS
    pass_ok = hmac.compare_digest(password.encode(), expected_password.encode())
    return username if user_ok and pass_ok else None


def issue_session(username: str) -> str:
    """Persist a hash of a random session token; the raw bearer token stays in cookie."""
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    created = datetime.now(timezone.utc)
    expires = created + timedelta(seconds=SESSION_TTL_SECONDS)
    with db_lock, connect() as db:
        db.execute("DELETE FROM operator_sessions WHERE expires_at<=?", (now_iso(),))
        db.execute("INSERT INTO operator_sessions(token_hash, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
                   (token_hash, username, created.isoformat(timespec="milliseconds"), expires.isoformat(timespec="milliseconds")))
    return token


def current_operator(token: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> str:
    """Resolve a valid, unexpired operator session cookie or return 401."""
    if not token:
        raise HTTPException(status_code=401, detail="Operator login required")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_lock, connect() as db:
        row = db.execute("SELECT username, expires_at FROM operator_sessions WHERE token_hash=?", (token_hash,)).fetchone()
        if row is None or row["expires_at"] <= now_iso():
            if row is not None:
                db.execute("DELETE FROM operator_sessions WHERE token_hash=?", (token_hash,))
            raise HTTPException(status_code=401, detail="Operator login required")
        return row["username"]


def revoke_session(token: str | None) -> None:
    """Delete a session using only its one-way token hash."""
    if not token:
        return
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with db_lock, connect() as db:
        db.execute("DELETE FROM operator_sessions WHERE token_hash=?", (token_hash,))


def clear_session_cookie(response: Response) -> None:
    """Expire the browser cookie using the same flags used when setting it."""
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="strict")


def require_intent(x_pulse_request: str | None = Header(default=None)) -> None:
    """Require an explicit same-origin intent marker on state-changing browser calls."""
    if x_pulse_request != "1":
        raise HTTPException(status_code=403, detail="Missing request intent header")
