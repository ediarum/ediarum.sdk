"""Exercises the debounced watcher with a plain recording callback."""

import time

import pytest

from ediarum_sdk.watch import Watcher

QUIET = 0.3


@pytest.fixture
def data(tmp_path):
    d = tmp_path / "mega"
    d.mkdir()
    (d / "a.xml").write_text("<p/>")
    return d


@pytest.fixture
def calls():
    return []


@pytest.fixture
def watcher(data, calls):
    w = Watcher({"mega": data}, on_due=lambda project, paths: calls.append((project, paths)), quiet_seconds=QUIET)
    w.start()
    time.sleep(0.2)  # let the observer settle
    w.due()  # drop anything left over from setup/settling
    yield w
    w.stop()


def step_until(watcher, condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        watcher.step()
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_new_file_triggers_callback(watcher, data, calls):
    (data / "sub").mkdir()
    (data / "sub" / "b.xml").write_text("<p/>")
    assert step_until(watcher, lambda: calls)
    [(project, paths)] = calls
    assert project == "mega"
    assert any(p.endswith("b.xml") for p in paths)


def test_burst_is_debounced_into_one_call(watcher, data, calls):
    for i in range(5):
        (data / f"f{i}.xml").write_text("<p/>")
        time.sleep(0.05)
    assert step_until(watcher, lambda: calls)
    time.sleep(QUIET * 2)
    watcher.step()  # a second call would show up here if debouncing failed
    assert len(calls) == 1
    assert len(calls[0][1]) == 5


def test_moved_file_triggers_callback(watcher, data, tmp_path, calls):
    outside = tmp_path / "incoming.tmp"
    outside.write_text("<p/>")
    outside.rename(data / "moved.xml")
    assert step_until(watcher, lambda: calls)
    assert any(p.endswith("moved.xml") for p in calls[0][1])


def test_reads_and_deletes_do_not_trigger(watcher, data):
    (data / "a.xml").read_bytes()
    (data / "a.xml").unlink()
    (data / "notes.txt").write_text("not xml")
    time.sleep(QUIET * 2)
    assert watcher.due() == {}


def test_dot_dir_is_ignored(watcher, data):
    (data / ".git").mkdir()
    (data / ".git" / "x.xml").write_text("<p/>")
    time.sleep(QUIET * 2)
    assert watcher.due() == {}


def test_on_due_exception_does_not_stop_the_watcher(data):
    calls = []

    def flaky(project, paths):
        calls.append((project, paths))
        raise RuntimeError("boom")

    w = Watcher({"mega": data}, on_due=flaky, quiet_seconds=QUIET)
    w.start()
    time.sleep(0.2)
    w.due()
    try:
        (data / "b.xml").write_text("<p/>")
        assert step_until(w, lambda: calls)
        # a second change should still be picked up after the first callback raised
        (data / "c.xml").write_text("<p/>")
        assert step_until(w, lambda: len(calls) == 2)
    finally:
        w.stop()


def test_missing_directory_is_skipped_not_fatal(tmp_path):
    w = Watcher({"ghost": tmp_path / "does-not-exist"}, on_due=lambda p, paths: None, quiet_seconds=QUIET)
    assert w.projects == {}
    with pytest.raises(SystemExit):
        w.start()
