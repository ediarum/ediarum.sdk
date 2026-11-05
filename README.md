# ediarum.sdk

Version: 0.3.1

© 2026 by Berlin-Brandenburg Academy of Sciences and Humanities

Part of ediarum (https://www.ediarum.org, ediarum@bbaw.de)

Developed by TELOTA (https://www.bbaw.de/telota, telota@bbaw.de), a DH
working group of the Berlin-Brandenburg Academy of Sciences and Humanities.

Developer of ediarum.sdk:

* Malte Vogl

Shared Python package for ediarum backend services (`ediarum.index`,
`ediarum.id`, `ediarum.fileservice`). It provides platform authentication,
a debounced file watcher and a client for the platform's project registry.

Requires Python 3.13 or later.

## Modules

| Module | Contents |
|---|---|
| `ediarum_sdk.auth` | Caller identification for FastAPI apps: platform auth-verify client (`platform_user`, `PlatformCallerDep`, `ProjectCallerDep`, `PlatformAdminDep`) and hashed API keys (`ApiKey`, `make_api_key_dependency`). Both produce a `Caller`. |
| `ediarum_sdk.watch` | `Watcher`: debounced, per-project filesystem watcher that calls `on_due(project, paths)` once a project has been quiet for `quiet_seconds`. |
| `ediarum_sdk.projects` | `ProjectsClient`: thread-safe, caching client for the platform's project list (`ProjectInfo`). Serves the last cached list if the platform is unreachable. |

## Configuration

| Variable | Used by | Purpose |
|---|---|---|
| `AUTH_VERIFY_URL` | `auth.AuthConfig.from_env()` | Platform auth-verify endpoint |
| `AUTH_DEV_USER` | `auth.AuthConfig.from_env()` | Treat every request as this admin user. Development only; mutually exclusive with `AUTH_VERIFY_URL` |
| `PLATFORM_PROJECTS_URL` | `projects.ProjectsClient` | Platform project registry, `…/api/auth/service/projects` (can also be passed as `url=`) |
| `PLATFORM_SERVICE_TOKEN` | `projects.ProjectsClient` | Service token for the registry, sent as `Authorization: Bearer` (can also be passed as `token=`) |

## Usage

```python
from pathlib import Path
from ediarum_sdk.watch import Watcher

def handle_changes(project: str, paths: set[str]) -> None:
    ...

Watcher({"demo": Path("/data/xml/demo")}, on_due=handle_changes).run_forever()
```

```python
import httpx2 as httpx
from ediarum_sdk.projects import ProjectsClient

projects = ProjectsClient(httpx.Client())
info = projects.get("demo")
```

## Notes

- The watcher is a supplementary trigger. File-change events can be lost;
  services should also run a periodic content-hash scan.
- The registry answers with `slug`, `name`, `repo_url`, `branch` and
  `index_source_of_truth` (`xml`/`app`: where ediarum.index takes the
  project's registers from). `ProjectInfo` maps `name` to `label`; `sparse_path` is `""` (whole repository) unless a row
  sets it.

## Installation

```
uv add ediarum-sdk
```

For development against unreleased changes, use a path dependency in
`[tool.uv.sources]`.

## Development

```
uv sync
uv run pytest
```

## Release

A release is a tag `vX.Y.Z` that matches `version` in `pyproject.toml`; CI
publishes it to PyPI.

1. On `main`, with a green pipeline: `uv version --bump patch` (or `minor`,
   `major`); set the same version in the `Version:` line above and in
   `CITATION.cff` (with `date-released`). Commit these four files.
2. `git tag -a v$(uv version --short) -m v$(uv version --short) && git push --follow-tags`
   (`--follow-tags` pushes annotated tags only).

   `scripts/release.sh patch|minor|major` does steps 1 and 2: it refuses
   unless on `main` with a clean tree, level with `origin/main` and the tag
   free, and asks before committing and pushing. It also does step 5.
3. The tag pipeline runs the tests, then `publish`: it refuses a tag that
   differs from the version, builds the package and uploads it with
   `uv publish` (the masked, protected CI/CD variable `UV_PUBLISH_TOKEN`).
4. In the apps: `uv lock --upgrade-package ediarum-sdk` (widen the version
   range in `pyproject.toml` for a new minor version).
5. Publish to GitHub (`github.com/ediarum/ediarum.sdk`): `scripts/publish.sh X.Y.Z`
   puts the release there as one snapshot commit "Release X.Y.Z" by TELOTA,
   tagged `vX.Y.Z`, without the internal history and without `.gitlab-ci.yml`.
   It refuses a snapshot with internal hosts or token-like strings
   (`FORBIDDEN` in the script). `scripts/release.sh` checks this before it
   changes anything and publishes right after the GitLab push; if only the
   GitHub push fails, rerun `scripts/publish.sh X.Y.Z`. `NO_GITHUB=1`
   releases on GitLab only. PyPI stays with the GitLab tag pipeline. Once per
   clone: `git remote add github git@github.com:ediarum/ediarum.sdk.git`.

Tags `v*` are protected (Settings → Repository → Protected tags): only
maintainers create releases, and protected variables such as
`UV_PUBLISH_TOKEN` are only available to protected refs.

## License

MIT, see the file `LICENSE`.

## Cite this software

If you use this software, please cite it using the metadata in the file
`CITATION.cff` (for more information about CFF see
https://citation-file-format.github.io/).
