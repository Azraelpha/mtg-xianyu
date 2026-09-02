import json

import pytest

from mtg_xianyu import storage


def test_atomic_write_text_replaces_target_and_cleans_temporary_file(tmp_path):
    target = tmp_path / "nested" / "output.txt"
    storage.atomic_write_text(target, "new content")

    assert target.read_text(encoding="utf-8") == "new content"
    assert list(target.parent.iterdir()) == [target]


def test_atomic_write_json_writes_strict_utf8_json(tmp_path):
    target = tmp_path / "output.json"
    storage.atomic_write_json(target, {"name": "玉玺", "price": 12.5})

    assert json.loads(target.read_text(encoding="utf-8")) == {
        "name": "玉玺",
        "price": 12.5,
    }
    assert "玉玺" in target.read_text(encoding="utf-8")


def test_atomic_write_preserves_target_and_cleans_temp_on_replace_failure(
    tmp_path, monkeypatch
):
    target = tmp_path / "output.txt"
    target.write_text("old content", encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        storage.atomic_write_text(target, "new content")

    assert target.read_text(encoding="utf-8") == "old content"
    assert list(tmp_path.iterdir()) == [target]


def test_prepare_failure_cleans_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "output.txt"

    def fail_fsync(_file_descriptor):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(storage.os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="simulated fsync failure"):
        storage.atomic_write_text(target, "new content")

    assert list(tmp_path.iterdir()) == []


def test_atomic_write_json_rejects_nonstandard_nan_without_touching_target(tmp_path):
    target = tmp_path / "output.json"
    target.write_text('{"old": true}', encoding="utf-8")

    with pytest.raises(ValueError, match="Out of range float values"):
        storage.atomic_write_json(target, {"price": float("nan")})

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
