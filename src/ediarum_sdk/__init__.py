"""Shared platform integration pieces for ediarum backend services.

Three modules, each independently usable:

- :mod:`ediarum_sdk.auth` — the platform auth-verify client (who sent this
  request, and are they allowed to use this project).
- :mod:`ediarum_sdk.watch` — a debounced filesystem watcher: collect changed
  files per project, call back once a project has been quiet for a bit.
- :mod:`ediarum_sdk.projects` — a typed, caching client for the platform's
  project registry (git coordinates, ``index_source_of_truth``).
"""
