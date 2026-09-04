import json
from pathlib import Path

import pytest

from mtg_xianyu import reconcile


def _write_listing(listings_dir: Path) -> tuple[Path, Path, dict]:
    listings_dir.mkdir()
    old_jpg = listings_dir / "WOT-74 - Necropotence - 英文平.jpg"
    old_jpg.write_bytes(b"jpeg")
    listing = {
        "row_id": "509556_0",
        "product_id": 509556,
        "name_en": "Necropotence (Anime Borderless)",
        "name_zh": "死冥权能",
        "set_code": "WOT",
        "set_name_zh": "艾卓仙踪魅附奇谭",
        "collector_number": "74",
        "printing": "Normal",
        "photo_jpg": str(old_jpg),
    }
    json_path = listings_dir / "509556_0.json"
    json_path.write_text(json.dumps(listing), encoding="utf-8")
    (listings_dir / "509556_0.txt").write_text("stale", encoding="utf-8")
    return json_path, old_jpg, listing


def test_plan_rejects_missing_listing_directory(tmp_path):
    with pytest.raises(ValueError, match="listing directory does not exist"):
        reconcile.plan_listing_updates(tmp_path / "missing")


def test_plan_detects_treatment_description_and_jpeg_drift(tmp_path):
    json_path, old_jpg, _ = _write_listing(tmp_path / "listings")

    updates = reconcile.plan_listing_updates(json_path.parent)

    assert len(updates) == 1
    update = updates[0]
    assert update.current_jpg == old_jpg
    assert update.desired_jpg.name == (
        "WOT-74 - Necropotence - 动漫无边框英文平.jpg"
    )
    assert "动漫无边框英文平" in update.description
    assert update.rename_jpg is True
    assert update.write_text is True
    assert old_jpg.is_file(), "planning must not change listing artifacts"


def test_apply_reconciles_jpeg_json_and_description(tmp_path):
    json_path, old_jpg, _ = _write_listing(tmp_path / "listings")
    update = reconcile.plan_listing_updates(json_path.parent)[0]

    reconcile.apply_listing_update(update)

    assert not old_jpg.exists()
    assert update.desired_jpg.is_file()
    revised = json.loads(json_path.read_text(encoding="utf-8"))
    assert revised["photo_jpg"] == str(update.desired_jpg)
    assert update.txt_path.read_text(encoding="utf-8") == update.description
    assert reconcile.plan_listing_updates(json_path.parent) == []


def test_json_failure_rolls_back_jpeg_rename(tmp_path, monkeypatch):
    json_path, old_jpg, listing = _write_listing(tmp_path / "listings")
    update = reconcile.plan_listing_updates(json_path.parent)[0]

    def fail_json_write(path, data):
        raise OSError("simulated JSON failure")

    monkeypatch.setattr(reconcile, "atomic_write_json", fail_json_write)
    with pytest.raises(OSError, match="simulated JSON failure"):
        reconcile.apply_listing_update(update)

    assert old_jpg.is_file()
    assert not update.desired_jpg.exists()
    assert json.loads(json_path.read_text(encoding="utf-8")) == listing
    assert update.txt_path.read_text(encoding="utf-8") == "stale"


def test_plan_uses_collision_suffix_for_occupied_desired_name(tmp_path):
    json_path, _, _ = _write_listing(tmp_path / "listings")
    desired = json_path.parent / "WOT-74 - Necropotence - 动漫无边框英文平.jpg"
    desired.write_bytes(b"other jpeg")

    update = reconcile.plan_listing_updates(json_path.parent)[0]

    assert update.desired_jpg.name == (
        "WOT-74 - Necropotence - 动漫无边框英文平 (copy 1).jpg"
    )


def test_plan_rejects_photo_path_outside_listing_directory(tmp_path):
    json_path, _, listing = _write_listing(tmp_path / "listings")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"jpeg")
    listing["photo_jpg"] = str(outside)
    json_path.write_text(json.dumps(listing), encoding="utf-8")

    with pytest.raises(ValueError, match="photo_jpg escapes"):
        reconcile.plan_listing_updates(json_path.parent)
