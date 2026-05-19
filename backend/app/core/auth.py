"""Identity resolution — HA Ingress headers and standalone JWT.

Two deployment modes (plan locked decision #1):

* **add-on** — Home Assistant's Ingress proxy injects
  ``X-Remote-User-Id`` / ``X-Remote-User-Name`` /
  ``X-Remote-User-Display-Name`` on every request (plan locked
  decision #7). We trust those headers because nothing reaches the
  add-on except through the Supervisor's Ingress.
* **standalone** — no Supervisor, so the app owns its own login: a
  signed JWT (``python-jose``) carried in ``Authorization: Bearer``.
  The token subject (``sub``) is the user id.

:func:`current_identity` is the FastAPI dependency every route uses to
learn *who* is calling. RBAC (:mod:`app.core.acl`) layers on top.

Telegram is a third identity channel: a chat_id is only authoritative
when :func:`resolve_telegram_user` maps it to a known HA user (plan
locked decision #9). Telegram-originated actions must audit *both* the
chat_id and the resolved HA user_id.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import HTTPException, Request, status
from jose import JWTError, jwt
from sqlalchemy import select

from app.config import Mode, get_settings
from app.models.telegram_user_map import TelegramUserMap
from app.models.user import RoleName, User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


# --- JWT constants -----------------------------------------------------

#: JWT signing algorithm. HS256 keeps standalone mode self-contained
#: (symmetric key, no keypair management).
JWT_ALGORITHM = "HS256"

#: Default access-token lifetime for standalone mode.
ACCESS_TOKEN_TTL = dt.timedelta(hours=12)

# Ingress headers injected by the HA Supervisor (plan locked decision #7).
HEADER_USER_ID = "X-Remote-User-Id"
HEADER_USER_NAME = "X-Remote-User-Name"
HEADER_USER_DISPLAY_NAME = "X-Remote-User-Display-Name"


@dataclass
class Identity:
    """The authenticated caller.

    Attributes:
        user_id: Stable user id — HA user id in add-on mode, JWT
            ``sub`` in standalone mode.
        display_name: Human-friendly name for UI / audit summaries.
        mode: Which auth path produced this identity.
        roles: Roles resolved for the user. Empty until
            :func:`app.core.acl.require_role` stamps it; an empty set
            means "treat as ``operator``".
    """

    user_id: str
    display_name: str
    mode: Mode
    roles: set[RoleName] = field(default_factory=set)


def _jwt_secret() -> str:
    """Return the JWT signing secret for standalone mode.

    Reuses the current HMAC key (already a 32-byte secret managed by
    the operator) so standalone mode needs no extra configuration. In
    add-on mode JWTs are never minted, so a missing key is only an
    error on the standalone path.
    """
    settings = get_settings()
    key = settings.hmac_keys.get(settings.hmac_key_id_current, "")
    if not key:
        raise RuntimeError(
            "no HMAC key configured; cannot sign/verify standalone JWTs"
        )
    return key


def create_access_token(
    user_id: str,
    *,
    expires_in: dt.timedelta | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Mint a signed JWT for *user_id* (standalone mode login).

    Args:
        user_id: Subject of the token (becomes :attr:`Identity.user_id`).
        expires_in: Token lifetime; defaults to :data:`ACCESS_TOKEN_TTL`.
        extra_claims: Optional extra claims merged into the payload
            (e.g. a display name).

    Returns:
        The encoded JWT string.
    """
    now = dt.datetime.now(dt.UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iat": now,
        "exp": now + (expires_in or ACCESS_TOKEN_TTL),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    """Validate and decode a standalone-mode JWT.

    Args:
        token: The raw JWT (no ``Bearer`` prefix).

    Returns:
        The decoded claims dict.

    Raises:
        fastapi.HTTPException: 401 if the token is malformed, expired,
            or fails signature verification.
    """
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except JWTError as exc:
        log.warning("jwt_invalid", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def _identity_from_headers(request: Request) -> Identity:
    """Build an :class:`Identity` from HA Ingress headers (add-on mode).

    Raises:
        fastapi.HTTPException: 401 if the mandatory ``X-Remote-User-Id``
            header is absent — that should never happen behind Ingress,
            so its absence means a misrouted / unauthenticated request.
    """
    user_id = request.headers.get(HEADER_USER_ID)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing X-Remote-User-Id (request did not arrive via Ingress)",
        )
    display_name = (
        request.headers.get(HEADER_USER_DISPLAY_NAME)
        or request.headers.get(HEADER_USER_NAME)
        or user_id
    )
    return Identity(user_id=user_id, display_name=display_name, mode="addon")


def _bearer_token(request: Request) -> str:
    """Extract the bearer token from the ``Authorization`` header."""
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization: Bearer header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def _identity_from_jwt(request: Request) -> Identity:
    """Build an :class:`Identity` from a bearer JWT (standalone mode)."""
    claims = decode_token(_bearer_token(request))
    user_id = claims.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="access token has no subject",
            headers={"WWW-Authenticate": "Bearer"},
        )
    display_name = str(claims.get("display_name") or user_id)
    return Identity(user_id=str(user_id), display_name=display_name, mode="standalone")


async def current_identity(request: Request) -> Identity:
    """FastAPI dependency — resolve the authenticated caller.

    Dispatches on :attr:`app.config.Settings.mode`: Ingress headers in
    add-on mode, a bearer JWT in standalone mode.

    Standalone dev escape hatch
    ---------------------------
    When ``OCS_DEV_AUTH`` is set, standalone mode treats every request
    as that ``users.id`` with no token — so the bundled UI is usable on
    a private box without a login flow. This is deliberately gated:

    * It is **only** consulted on the standalone branch. The regulated
      facility runs the HA add-on (``SUPERVISOR_TOKEN`` present ->
      add-on mode), which returns above before this code is reached —
      the bypass is structurally unreachable there.
    * It is off unless the operator explicitly sets the env var.
    * Every bypassed request logs a warning.

    Args:
        request: The incoming request (FastAPI injects it).

    Returns:
        The resolved :class:`Identity`.

    Raises:
        fastapi.HTTPException: 401 when the caller cannot be identified.
    """
    if get_settings().mode == "addon":
        return _identity_from_headers(request)

    dev_user = get_settings().ocs_dev_auth.strip()
    if dev_user:
        log.warning("dev_auth_bypass", user_id=dev_user, path=request.url.path)
        return Identity(
            user_id=dev_user, display_name=dev_user, mode="standalone"
        )

    return _identity_from_jwt(request)


async def resolve_telegram_user(
    session: AsyncSession, chat_id: str | int
) -> User | None:
    """Resolve a Telegram chat_id to its mapped HA :class:`User`.

    Telegram approvals are authoritative only when the originating
    chat_id is mapped here (plan locked decision #9). The SFW phase
    calls this to verify approval authority; an unmapped chat_id
    resolves to ``None`` and the caller must reject + audit.

    Args:
        session: Active async session.
        chat_id: Telegram chat id (stored as a string in the map).

    Returns:
        The mapped :class:`User`, or ``None`` if the chat_id is not
        mapped or the mapped user no longer exists.
    """
    mapping = await session.get(TelegramUserMap, str(chat_id))
    if mapping is None:
        log.warning("telegram_unmapped_chat", chat_id=str(chat_id))
        return None
    result = await session.execute(
        select(User).where(User.id == mapping.user_id)
    )
    return result.scalar_one_or_none()
