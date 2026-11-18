"""Operator authentication and write-request intent checks."""
import hmac

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import OPERATOR_USERS

security = HTTPBasic()


def basic_operator(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    """Authenticate with constant-time password comparison and generic failures."""
    # Use a dummy password for unknown users so response work is similar for both cases.
    expected_password = OPERATOR_USERS.get(credentials.username, "invalid-operator-password")
    user_ok = credentials.username in OPERATOR_USERS
    pass_ok = hmac.compare_digest(credentials.password.encode(), expected_password.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Operator authentication required", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def require_intent(x_pulse_request: str | None = Header(default=None)) -> None:
    """Require an explicit same-origin intent marker on state-changing browser calls."""
    if x_pulse_request != "1":
        raise HTTPException(status_code=403, detail="Missing request intent header")
