"""A typed, caching client for the platform's project registry.

The platform owns the canonical project list — slug, git coordinates, and
``index_source_of_truth`` (where ediarum.index takes the project's registers
from: ``xml`` or ``app``; it says nothing about the texts); services read it through this client instead of each
keeping local config.

The platform serves it at ``GET /api/auth/service/projects``
(``PLATFORM_PROJECTS_URL``), authenticated with a service token
(``PLATFORM_SERVICE_TOKEN``, sent as ``Authorization: Bearer``). Rows look like
``{"slug", "name", "repo_url", "branch", "index_source_of_truth"}``;
:meth:`ProjectInfo.from_json` also accepts ``label``, ``sparse_path`` and the
field's name before 0.3, ``source_of_truth`` (static files, tests).

A platform outage degrades to the last successful fetch rather than failing
every dependent request — see :meth:`ProjectsClient.all`.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Literal

import httpx2 as httpx

PROJECTS_URL_ENV = "PLATFORM_PROJECTS_URL"
PROJECTS_TOKEN_ENV = "PLATFORM_SERVICE_TOKEN"
DEFAULT_CACHE_TTL = 60.0  # seconds
DEFAULT_TIMEOUT = 5.0

log = logging.getLogger("uvicorn.error")

SourceOfTruth = Literal["xml", "app"]


class ConfigError(Exception):
    "Invalid or missing environment configuration."


@dataclass(frozen=True)
class ProjectInfo:
    slug: str
    label: str
    repo_url: str
    branch: str
    index_source_of_truth: SourceOfTruth  # ediarum.index's registers only
    sparse_path: str = ""  # "" = the whole repository

    @classmethod
    def from_json(cls, data: dict) -> "ProjectInfo":
        return cls(
            slug=data["slug"],
            label=data.get("label") or data.get("name") or data["slug"],
            repo_url=data["repo_url"],
            branch=data["branch"],
            index_source_of_truth=data.get("index_source_of_truth") or data["source_of_truth"],
            sparse_path=(data.get("sparse_path") or "").strip("/"),
        )


class ProjectsClient:
    """Fetches and caches the platform's project list.

    Thread-safe: ``all()``/``get()`` may be called from request handlers and
    from a background watcher/reconciler loop concurrently.
    """

    def __init__(self, client: httpx.Client, url: str | None = None, ttl: float = DEFAULT_CACHE_TTL,
                 token: str | None = None):
        self.client = client
        self.url = url or os.getenv(PROJECTS_URL_ENV)
        if not self.url:
            raise ConfigError(f"set {PROJECTS_URL_ENV}, or pass url=")
        token = token or os.getenv(PROJECTS_TOKEN_ENV)
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cached: tuple[float, dict[str, ProjectInfo]] | None = None

    def _fetch(self) -> dict[str, ProjectInfo]:
        res = self.client.get(self.url, headers=self.headers, timeout=DEFAULT_TIMEOUT)
        res.raise_for_status()
        projects = [ProjectInfo.from_json(row) for row in res.json()]
        return {p.slug: p for p in projects}

    def all(self, *, force: bool = False) -> dict[str, ProjectInfo]:
        """Every known project, keyed by slug.

        Cached for ``ttl`` seconds. On fetch failure, serves the last good
        value if one exists (logging a warning) rather than raising — so a
        platform hiccup degrades to a stale project list instead of
        cascading into every dependent request failing. Raises only if there
        is no cached value yet (e.g. on startup, before any successful
        fetch) — callers that can't proceed without a fresh list pass
        ``force=True``.
        """
        with self._lock:
            now = time.monotonic()
            if not force and self._cached is not None and now - self._cached[0] < self.ttl:
                return self._cached[1]
            try:
                projects = self._fetch()
            except httpx.HTTPError as exc:
                if self._cached is not None:
                    log.warning("platform project registry unreachable (%r); serving cached list", exc)
                    return self._cached[1]
                raise
            self._cached = (now, projects)
            return projects

    def get(self, slug: str, *, force: bool = False) -> ProjectInfo | None:
        "One project by slug, or ``None`` if the platform doesn't know it."
        return self.all(force=force).get(slug)
