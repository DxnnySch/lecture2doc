import json
import logging
import os

import pytest

import fsutil
from fsutil import atomic_write_json


def test_a_briefly_locked_target_is_retried(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    real_replace, calls, waits = os.replace, [], []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) < 3:  # antivirus / indexer / cloud sync holding the file for a moment
            raise PermissionError(13, "Zugriff verweigert")
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    atomic_write_json(path, {"a": 1}, sleep=waits.append)
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert len(calls) == 3 and len(waits) == 2 and waits == sorted(waits)  # waits grow
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


def test_a_target_that_stays_locked_raises_after_the_last_attempt_and_leaves_no_temp_file(tmp_path, monkeypatch):
    path = tmp_path / "c.json"
    path.write_text('{"old": true}', encoding="utf-8")

    def locked(src, dst):
        raise PermissionError(13, "Zugriff verweigert")

    monkeypatch.setattr(os, "replace", locked)
    with pytest.raises(PermissionError):
        atomic_write_json(path, {"new": 1}, attempts=4, sleep=lambda s: None)
    monkeypatch.undo()
    assert json.loads(path.read_text(encoding="utf-8")) == {"old": True}
    assert [p.name for p in tmp_path.iterdir()] == ["c.json"]


def test_other_errors_are_not_retried(tmp_path, monkeypatch):
    calls = []

    def full(src, dst):
        calls.append(1)
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", full)
    with pytest.raises(OSError, match="disk full"):
        atomic_write_json(tmp_path / "c.json", {}, sleep=lambda s: pytest.fail("must not wait"))
    assert len(calls) == 1


def test_default_waits_stay_short(monkeypatch, tmp_path):
    waits = []
    monkeypatch.setattr(os, "replace", lambda s, d: (_ for _ in ()).throw(PermissionError(13, "x")))
    with pytest.raises(PermissionError):
        atomic_write_json(tmp_path / "c.json", {}, sleep=waits.append)
    assert sum(waits) < 5  # a stuck file must not hold up the run for long
    assert fsutil.ATTEMPTS >= 4
