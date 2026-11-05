"""Exercises the project-registry client: caching, forced refresh, and
falling back to the last good value on a platform outage.
"""

import httpx2 as httpx
import pytest

from ediarum_sdk.projects import ConfigError, ProjectsClient

URL = "http://platform.test/projects"

ROW = {
    "slug": "mega",
    "label": "MEGA",
    "repo_url": "git@example.org:group/project-data.git",
    "branch": "main",
    "sparse_path": "data",
    "source_of_truth": "xml",  # the field's name before 0.3, still accepted
}


class Provider:
    def __init__(self, rows):
        self.rows = rows
        self.calls = 0
        self.fail = False
        self.last_request = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_request = request
        if self.fail:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=self.rows)


@pytest.fixture
def provider():
    return Provider([ROW])


@pytest.fixture
def http_client(provider):
    return httpx.Client(transport=httpx.MockTransport(provider))


def test_fetches_and_parses(http_client, provider):
    client = ProjectsClient(http_client, url=URL)
    projects = client.all()
    assert set(projects) == {"mega"}
    mega = projects["mega"]
    assert (mega.slug, mega.branch, mega.index_source_of_truth) == ("mega", "main", "xml")
    assert provider.calls == 1


def test_get_one_by_slug(http_client):
    client = ProjectsClient(http_client, url=URL)
    assert client.get("mega").slug == "mega"
    assert client.get("nonexistent") is None


def test_result_is_cached_within_ttl(http_client, provider):
    client = ProjectsClient(http_client, url=URL, ttl=60.0)
    client.all()
    client.all()
    client.all()
    assert provider.calls == 1


def test_force_bypasses_cache(http_client, provider):
    client = ProjectsClient(http_client, url=URL, ttl=60.0)
    client.all()
    client.all(force=True)
    assert provider.calls == 2


def test_outage_serves_last_known_good(http_client, provider):
    client = ProjectsClient(http_client, url=URL, ttl=0.0)  # always stale, so every call refetches
    good = client.all()
    provider.fail = True
    stale = client.all()
    assert stale == good


def test_outage_before_any_success_raises(http_client, provider):
    provider.fail = True
    client = ProjectsClient(http_client, url=URL)
    with pytest.raises(httpx.HTTPError):
        client.all()


def test_missing_url_is_a_config_error(http_client, monkeypatch):
    monkeypatch.delenv("PLATFORM_PROJECTS_URL", raising=False)
    with pytest.raises(ConfigError):
        ProjectsClient(http_client)


def test_url_from_env(http_client, monkeypatch, provider):
    monkeypatch.setenv("PLATFORM_PROJECTS_URL", URL)
    client = ProjectsClient(http_client)
    assert client.all()
    assert provider.calls == 1


def test_sends_service_token(http_client, provider, monkeypatch):
    monkeypatch.delenv("PLATFORM_SERVICE_TOKEN", raising=False)
    ProjectsClient(http_client, url=URL, token="secret").all()
    assert provider.last_request.headers["authorization"] == "Bearer secret"


def test_service_token_from_env(http_client, provider, monkeypatch):
    monkeypatch.setenv("PLATFORM_SERVICE_TOKEN", "from-env")
    ProjectsClient(http_client, url=URL).all()
    assert provider.last_request.headers["authorization"] == "Bearer from-env"


def test_no_token_no_header(http_client, provider, monkeypatch):
    monkeypatch.delenv("PLATFORM_SERVICE_TOKEN", raising=False)
    ProjectsClient(http_client, url=URL).all()
    assert "authorization" not in provider.last_request.headers


def test_label_falls_back_to_platform_name(http_client, provider):
    provider.rows = [{k: v for k, v in ROW.items() if k != "label"} | {"name": "MEGA (platform)"}]
    assert ProjectsClient(http_client, url=URL).get("mega").label == "MEGA (platform)"


PLATFORM_ROW = {
    "slug": "schleiermacher",
    "name": "Schleiermacher",
    "repo_url": "git@example.org:group/schleiermacher-data.git",
    "branch": "ediarum-webdav",
    "index_source_of_truth": "xml",
}


def test_parses_platform_rows(http_client, provider):
    "The shape GET /api/auth/service/projects answers with."
    provider.rows = [PLATFORM_ROW]
    info = ProjectsClient(http_client, url=URL).get("schleiermacher")
    assert info.label == "Schleiermacher"
    assert info.index_source_of_truth == "xml"
    assert info.branch == "ediarum-webdav"
    assert info.sparse_path == ""
