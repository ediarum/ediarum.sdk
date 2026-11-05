"""Exercises the platform auth-verify client against a stub provider."""

import httpx2 as httpx
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from typing import Annotated

from ediarum_sdk.auth import (
    AuthConfig,
    Caller,
    PlatformAdminDep,
    ProjectCallerDep,
    platform_user,
)

VERIFY_URL = "http://auth.test/api/auth/verify"
# session cookie value -> (email, roles, projects), as the auth provider would answer
USERS = {
    "ada": ("ada@example.org", "", "mega"),
    "bob": ("bob@example.org", "", "other"),
    "admin": ("admin@example.org", "admin", ""),
}


class Provider:
    "A stub auth provider: answers verify like the contract says and records what it got."

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.answer: int | None = None
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            raise httpx.ConnectError("down", request=request)
        if self.answer is not None:
            return httpx.Response(self.answer)
        cookie = request.headers.get("cookie", "")
        user = USERS.get(cookie.removeprefix("session="))
        if user is None:
            return httpx.Response(401)
        method = request.headers.get("x-original-method", "GET")
        if method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-xsrf-token") != "tok":
            return httpx.Response(419)
        email, roles, projects = user
        return httpx.Response(204, headers={"X-User": email, "X-User-Roles": roles, "X-User-Projects": projects})


@pytest.fixture
def provider():
    return Provider()


@pytest.fixture
def app(provider):
    app = FastAPI()
    app.state.auth_config = AuthConfig(verify_url=VERIFY_URL)
    app.state.verify_client = httpx.Client(transport=httpx.MockTransport(provider))

    @app.get("/whoami")
    def whoami(caller: Annotated[Caller, Depends(platform_user)]):
        return {"name": caller.name, "is_admin": caller.is_admin, "projects": caller.projects}

    @app.post("/whoami")
    def whoami_write(caller: Annotated[Caller, Depends(platform_user)]):
        return {"name": caller.name}

    @app.get("/{project}/thing")
    def thing(project: str, caller: ProjectCallerDep):
        return {"ok": True}

    @app.get("/admin-only")
    def admin_only_route(caller: PlatformAdminDep):
        return {"ok": True}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def as_user(name: str, write: bool = False) -> dict:
    headers = {"Cookie": f"session={name}"}
    if write:
        headers["X-XSRF-TOKEN"] = "tok"
    return headers


def test_known_user_is_identified(client, provider):
    r = client.get("/whoami", headers=as_user("ada"))
    assert r.status_code == 200
    assert r.json() == {"name": "ada@example.org", "is_admin": False, "projects": ["mega"]}
    sent = provider.requests[-1]
    assert str(sent.url) == VERIFY_URL
    assert sent.headers["cookie"] == "session=ada"


def test_no_cookie_is_401(client):
    assert client.get("/whoami").status_code == 401


def test_write_without_csrf_token_is_419(client):
    r = client.post("/whoami", headers={"Cookie": "session=ada"})
    assert r.status_code == 419


def test_write_with_csrf_token_succeeds(client):
    r = client.post("/whoami", headers=as_user("ada", write=True))
    assert r.status_code == 200


def test_member_may_use_their_project(client):
    assert client.get("/mega/thing", headers=as_user("ada")).status_code == 200


def test_non_member_is_refused(client):
    assert client.get("/other/thing", headers=as_user("ada")).status_code == 403
    assert client.get("/mega/thing", headers=as_user("bob")).status_code == 403


def test_admin_may_use_every_project(client):
    assert client.get("/other/thing", headers=as_user("admin")).status_code == 200


def test_admin_only_route_refuses_non_admin(client):
    assert client.get("/admin-only", headers=as_user("ada")).status_code == 403
    assert client.get("/admin-only", headers=as_user("admin")).status_code == 200


def test_provider_down_is_503(client, provider):
    provider.fail = True
    assert client.get("/whoami", headers=as_user("ada")).status_code == 503


def test_dev_user_bypasses_the_provider(provider):
    app = FastAPI()
    app.state.auth_config = AuthConfig(dev_user="dev@example.org")
    app.state.verify_client = httpx.Client(transport=httpx.MockTransport(provider))

    @app.get("/whoami")
    def whoami(caller: Annotated[Caller, Depends(platform_user)]):
        return {"name": caller.name, "is_admin": caller.is_admin}

    r = TestClient(app).get("/whoami")
    assert r.status_code == 200
    assert r.json() == {"name": "dev@example.org", "is_admin": True}
    assert provider.requests == []  # never actually asked


def test_both_verify_url_and_dev_user_is_a_config_error(monkeypatch):
    from ediarum_sdk.auth import ConfigError

    monkeypatch.setenv("AUTH_VERIFY_URL", VERIFY_URL)
    monkeypatch.setenv("AUTH_DEV_USER", "dev@example.org")
    with pytest.raises(ConfigError):
        AuthConfig.from_env()
