"""Durable filesystem writes shared by pipeline stages and the review UI."""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def prepare_text_file(target: Path, payload: str) -> Path:
    """Write and fsync a temporary sibling ready for an atomic replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            delete=False,
        ) as tmp:
            temp_path = Path(tmp.name)
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        assert temp_path is not None
        return temp_path
    except Exception:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(target: Path, payload: str) -> None:
    """Atomically replace target with UTF-8 text, preserving it on failure."""
    temp_path: Path | None = None
    try:
        temp_path = prepare_text_file(target, payload)
        os.replace(temp_path, target)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def atomic_write_json(target: Path, data: Any) -> None:
    """Serialize strict JSON and atomically replace target."""
    payload = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)
    atomic_write_text(target, payload)
