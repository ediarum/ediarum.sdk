"""Two independent ways to identify a caller, composable rather than
unified — an app uses whichever (or both, on different routes) its callers
actually need:

1. **The platform auth-verify client** — for a human in a browser:
   :func:`platform_user` asks the platform's central auth provider who sent
   a cookie-bearing request. Two things an app wires up before using this:
   ``request.app.state.auth_config`` (an :class:`AuthConfig`, typically
   ``AuthConfig.from_env()``, set during startup) and
   ``request.app.state.verify_client`` (a shared, keep-alive
   ``httpx.Client`` — one per process, not one per request).

2. **Hashed API keys** — for a *system* acting for itself: no browser, no
   session, nothing to verify against the platform (e.g. another service
   or a script). :func:`make_api_key_dependency` builds a FastAPI
   dependency, closed over the app's own session dependency, that resolves
   an ``Authorization: Bearer``/``X-API-Key``/``?key=`` credential against
   the :class:`ApiKey` table this module also defines. A key is scoped to
   a list of projects, or ``"*"`` for every project, via
   :attr:`ApiKey.projects`.

Both halves produce the same :class:`Caller`, discriminated by `.kind`.
"""

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal

import httpx2 as httpx
from fastapi import Depends, HTTPException, Query, Request, status
from sqlmodel import Field, Session, SQLModel, select


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


VERIFY_URL_ENV = "AUTH_VERIFY_URL"
DEV_USER_ENV = "AUTH_DEV_USER"
VERIFY_TIMEOUT = 5.0
ADMIN_ROLE = "admin"

log = logging.getLogger("uvicorn.error")


class ConfigError(Exception):
    "Invalid or missing environment configuration."


# --- settings ---------------------------------------------------------------

@dataclass(frozen=True)
class AuthConfig:
    verify_url: str | None = None
    dev_user: str | None = None  # every request is this admin user; never in production

    @classmethod
    def from_env(cls) -> "AuthConfig":
        verify_url = os.getenv(VERIFY_URL_ENV) or None
        dev_user = os.getenv(DEV_USER_ENV) or None
        if verify_url and dev_user:
            raise ConfigError(f"set either {VERIFY_URL_ENV} or {DEV_USER_ENV}, not both")
        return cls(verify_url, dev_user)


UNCONFIGURED = AuthConfig()


# --- callers ------------------------------------------------------------

@dataclass(frozen=True)
class Caller:
    name: str  # the user's email (or a key client's name, for an app that adds key auth)
    kind: Literal["key", "user"]
    is_admin: bool  # admin key, or user with the role `admin`
    projects: tuple[str, ...] | None  # None = every project

    def may_use(self, project: str) -> bool:
        return self.is_admin or self.projects is None or project in self.projects


def split(value: str) -> tuple[str, ...]:
    return tuple(v for v in value.split(",") if v)


def verify_user(client: httpx.Client, url: str, request: Request) -> Caller:
    "Ask the auth provider who sent ``request``; mapped exactly as the auth contract says."
    headers = {"X-Original-Method": request.method}
    for name in ("cookie", "x-xsrf-token"):
        if (value := request.headers.get(name)) is not None:
            headers[name] = value
    try:
        res = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        log.error("auth verify request failed: %r", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication service unavailable")
    if res.status_code == 204 and res.headers.get("x-user"):
        roles = split(res.headers.get("x-user-roles", ""))
        return Caller(res.headers["x-user"], "user", ADMIN_ROLE in roles,
                      split(res.headers.get("x-user-projects", "")))
    if res.status_code == 401:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not logged in")
    if res.status_code == 419:
        raise HTTPException(419, "CSRF token missing or expired")
    log.error("auth verify answered %s%s", res.status_code, " without X-User" if res.status_code == 204 else "")
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "authentication service unavailable")


def platform_user(request: Request) -> Caller:
    """The platform user who sent ``request`` — a FastAPI dependency.

    An app that *only* needs platform-user auth (no API keys) uses this
    directly via :data:`PlatformCallerDep`. An app that wants *both* (e.g.
    platform users in an admin UI, API keys for scripts) composes :func:`make_api_key_dependency` with this::

        def current_caller(request: Request, session: SessionDep,
                            key: str | None = Query(None, include_in_schema=False)) -> Caller:
            presented = extract_key(request, key)
            return key_caller(session, presented) if presented else platform_user(request)

    An app whose callers are *only* API keys (e.g. an endpoint never called
    from a browser) skips `platform_user`
    entirely and uses :func:`make_api_key_dependency` on its own.
    """
    config: AuthConfig = getattr(request.app.state, "auth_config", UNCONFIGURED)
    if config.dev_user:
        return Caller(config.dev_user, "user", True, ())
    if not config.verify_url:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing api key")
    return verify_user(request.app.state.verify_client, config.verify_url, request)


def require_project_access(caller: Caller, project: str | None) -> None:
    "Raise 403 if ``project`` is set and ``caller`` may not use it."
    if project is not None and not caller.may_use(project):
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"no access to project {project}")


def platform_caller_for_project(request: Request) -> Caller:
    """:func:`platform_user`, plus a 403 if the request's ``project`` path
    parameter isn't one ``caller`` may use. Use this instead of
    :func:`platform_user` for routes under ``/{project}/...``.
    """
    caller = platform_user(request)
    require_project_access(caller, request.path_params.get("project"))
    return caller


def admin_only(caller: Annotated[Caller, Depends(platform_user)]) -> Caller:
    "Platform users with the role ``admin`` only."
    if caller.kind != "user" or not caller.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "platform admins only")
    return caller


PlatformCallerDep = Annotated[Caller, Depends(platform_user)]
ProjectCallerDep = Annotated[Caller, Depends(platform_caller_for_project)]
PlatformAdminDep = Annotated[Caller, Depends(admin_only)]


# --- API keys -----------------------------------------------------------

class ApiKey(SQLModel, table=True):
    "A client allowed to call this app's API. Only a hash of the key is stored."

    __tablename__ = "sdk_api_key"

    name: str = Field(primary_key=True, max_length=128)
    key_hash: str = Field(max_length=64, unique=True, index=True)
    projects: str = Field(default="*", max_length=1024)  # comma-separated project slugs, or "*"
    is_admin: bool = Field(default=False)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=utcnow)
    created_by: str | None = Field(default=None, max_length=255)  # platform admin who made it

    def may_use(self, project: str) -> bool:
        return self.projects == "*" or project in self.projects.split(",")


def hash_key(key: str) -> str:
    # keys are 256-bit random tokens, so a plain hash is sufficient (no salt/KDF needed)
    return hashlib.sha256(key.encode()).hexdigest()


def new_key() -> str:
    return secrets.token_urlsafe(32)


def create_key(
    session: Session, name: str, projects: str = "*", is_admin: bool = False, created_by: str | None = None
) -> str:
    "Create a key and return its plaintext — the only time it's visible; only `key_hash` is ever stored."
    key = new_key()
    session.add(ApiKey(name=name, key_hash=hash_key(key), projects=projects, is_admin=is_admin, created_by=created_by))
    session.commit()
    return key


def caller_from_key(api_key: ApiKey) -> Caller:
    projects = None if api_key.projects == "*" else tuple(api_key.projects.split(","))
    return Caller(api_key.name, "key", api_key.is_admin, projects)


def key_caller(session: Session, key: str) -> Caller:
    "Look up `key` (plaintext) against stored hashes; 401 if unknown or deactivated."
    api_key = session.exec(select(ApiKey).where(ApiKey.key_hash == hash_key(key))).first()
    if api_key is None or not api_key.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")
    return caller_from_key(api_key)


def extract_key(request: Request, key: str | None = None) -> str | None:
    "`Authorization: Bearer`, then `X-API-Key`, then a `key` query param — whichever is present first."
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[len("bearer "):].strip()
    if header_key := request.headers.get("x-api-key"):
        return header_key
    return key


def make_api_key_dependency(get_session):
    """Build a FastAPI dependency resolving the presented key via `get_session`
    — an app's own session dependency, since this module doesn't own any
    app's database engine. Use it directly for an API-key-only app, or
    compose it with :func:`platform_user` for an app that wants both — see
    :func:`platform_user`'s docstring for both shapes.
    """

    def api_key_caller(
        request: Request,
        session: Annotated[Session, Depends(get_session)],
        key: Annotated[str | None, Query(include_in_schema=False)] = None,
    ) -> Caller:
        presented = extract_key(request, key)
        if not presented:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing api key")
        return key_caller(session, presented)

    return api_key_caller
