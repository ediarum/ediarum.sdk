"""Exercises the hashed API-key mechanics — create_key/key_caller directly,
and make_api_key_dependency wired into a minimal FastAPI app.
"""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from ediarum_sdk.auth import ApiKey, Caller, create_key, key_caller, make_api_key_dependency


@pytest.fixture
def session():
    # StaticPool: the HTTP-backed tests below route the dependency through
    # FastAPI's threadpool, and a bare `sqlite://` in-memory DB is otherwise
    # connection-local -- a different thread would see an empty database.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


# --- create_key / key_caller, no HTTP ------------------------------------

def test_create_key_returns_plaintext_once(session):
    key = create_key(session, "service-key", projects="mega")
    assert len(key) > 20
    stored = session.get(ApiKey, "service-key")
    assert stored.key_hash != key  # only the hash is stored


def test_key_caller_resolves_scoped_key(session):
    key = create_key(session, "service-key", projects="mega")
    caller = key_caller(session, key)
    assert caller == Caller("service-key", "key", False, ("mega",))
    assert caller.may_use("mega")
    assert not caller.may_use("other")


def test_key_caller_resolves_admin_key(session):
    key = create_key(session, "admin-script", is_admin=True)
    caller = key_caller(session, key)
    assert caller.is_admin
    assert caller.may_use("anything")


def test_key_caller_rejects_unknown_key(session):
    with pytest.raises(Exception) as exc_info:
        key_caller(session, "not-a-real-key")
    assert exc_info.value.status_code == 401


def test_key_caller_rejects_deactivated_key(session):
    key = create_key(session, "revoked", projects="mega")
    stored = session.get(ApiKey, "revoked")
    stored.is_active = False
    session.add(stored)
    session.commit()
    with pytest.raises(Exception) as exc_info:
        key_caller(session, key)
    assert exc_info.value.status_code == 401


# --- make_api_key_dependency, through a real app --------------------------

@pytest.fixture
def app(session):
    app = FastAPI()
    dep = make_api_key_dependency(lambda: session)

    @app.get("/whoami")
    def whoami(caller: Annotated[Caller, Depends(dep)]):
        return {"name": caller.name, "projects": caller.projects}

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_bearer_header_is_accepted(client, session):
    key = create_key(session, "service-key", projects="mega")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {key}"})
    assert r.status_code == 200
    assert r.json() == {"name": "service-key", "projects": ["mega"]}


def test_x_api_key_header_is_accepted(client, session):
    key = create_key(session, "script", projects="mega")
    r = client.get("/whoami", headers={"X-API-Key": key})
    assert r.status_code == 200


def test_query_param_is_accepted(client, session):
    key = create_key(session, "script", projects="mega")
    r = client.get(f"/whoami?key={key}")
    assert r.status_code == 200


def test_bearer_takes_precedence_over_others(client, session):
    good = create_key(session, "good", projects="mega")
    create_key(session, "bad", projects="other")
    r = client.get("/whoami", headers={"Authorization": f"Bearer {good}", "X-API-Key": "wrong"})
    assert r.status_code == 200
    assert r.json()["name"] == "good"


def test_missing_key_is_401(client):
    assert client.get("/whoami").status_code == 401


def test_wrong_key_is_401(client, session):
    create_key(session, "service-key", projects="mega")
    r = client.get("/whoami", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
