"""A debounced, per-project filesystem watcher.

What a project's quiet period triggers is entirely up to the caller,
via a single callback, ``on_due``.

Usage::

    def handle_changes(project: str, paths: set[str]) -> None:
        ...  # re-ingest, rescan, whatever this service does

    watcher = Watcher({"demo": Path("/data/xml/demo")}, on_due=handle_changes)
    watcher.run_forever()

Design notes:

- Every created/modified/moved/closed file is collected per project. Once a
  project has been quiet for ``quiet_seconds`` (a ``git pull`` or a batch
  export touches many files at once), ``on_due`` is called once with every
  path collected since the last call.
- Read-only access (``opened``/``closed_no_write`` events, e.g. from the
  caller's own re-read of the files) is ignored — otherwise every read would
  queue the next watch cycle, forever.
- By default only ``*.xml`` files outside dot-directories (``.git``) are
  watched; pass ``file_filter`` to change that.
- File-change events can be lost (e.g. while a service is down), so this
  watcher must not be the only trigger; pair it with a periodic
  content-hash scan.
"""

import os
import threading
import time
from pathlib import Path
from typing import Callable

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

DEFAULT_QUIET_SECONDS = 10
WRITE_EVENTS = {"created", "modified", "moved", "closed"}

OnDue = Callable[[str, set[str]], None]
FileFilter = Callable[[str, Path], bool]


def is_xml_file(path: str, base: Path, suffix: str = ".xml") -> bool:
    "A file below ``base`` with the given ``suffix``, not inside a dot-directory such as ``.git``."
    try:
        parts = Path(path).relative_to(base).parts
    except ValueError:
        return False
    return path.endswith(suffix) and not any(p.startswith(".") for p in parts[:-1])


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


class _ChangeHandler(FileSystemEventHandler):
    def __init__(self, watcher: "Watcher", project: str, root: Path):
        self.watcher = watcher
        self.project = project
        self.root = root

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory or event.event_type not in WRITE_EVENTS:
            return
        paths = [event.src_path, getattr(event, "dest_path", "")]
        paths = [os.fsdecode(p) for p in paths if p]
        paths = [p for p in paths if self.watcher.file_filter(p, self.root)]
        if paths:
            self.watcher.touch(self.project, paths)


class Watcher:
    def __init__(
        self,
        projects: dict[str, Path],
        on_due: OnDue,
        quiet_seconds: float = DEFAULT_QUIET_SECONDS,
        file_filter: FileFilter = is_xml_file,
    ):
        self.on_due = on_due
        self.quiet_seconds = quiet_seconds
        self.file_filter = file_filter
        self.projects: dict[str, Path] = {}
        for name, root in projects.items():
            if not root.is_dir():
                log(f"{name}: directory {root} not found — not watched")
                continue
            self.projects[name] = root
        self._lock = threading.Lock()
        self._pending: dict[str, tuple[float, set[str]]] = {}  # project -> (last event, paths)
        self._observer = Observer()

    def touch(self, project: str, paths: list[str]) -> None:
        "Record changed files of a project (called from the observer thread)."
        with self._lock:
            _, pending = self._pending.get(project, (0.0, set()))
            pending.update(paths)
            self._pending[project] = (time.monotonic(), pending)

    def due(self) -> dict[str, set[str]]:
        "Take the pending changes of every project that has been quiet long enough."
        now = time.monotonic()
        with self._lock:
            ready = [p for p, (t, _) in self._pending.items() if now - t >= self.quiet_seconds]
            return {p: self._pending.pop(p)[1] for p in ready}

    def start(self) -> None:
        if not self.projects:
            raise SystemExit("nothing to watch — no configured directory exists")
        for name, root in self.projects.items():
            self._observer.schedule(_ChangeHandler(self, name, root), str(root), recursive=True)
            log(f"{name}: watching {root}")
        self._observer.start()

    def step(self) -> None:
        "Call ``on_due`` for every project whose changes are due."
        for project, paths in self.due().items():
            try:
                self.on_due(project, paths)
            except Exception as exc:  # keep watching whatever happens
                log(f"{project}: on_due failed: {type(exc).__name__}: {exc}")

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()

    def run_forever(self) -> None:
        self.start()
        log(f"watching {len(self.projects)} project(s), quiet period {self.quiet_seconds}s")
        try:
            while True:
                self.step()
                time.sleep(1)
        except KeyboardInterrupt:
            log("stopping ...")
        finally:
            self.stop()
