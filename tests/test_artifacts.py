from pathlib import Path

import pytest

from mtg_xianyu.artifacts import (
    confined_listing_path,
    jpg_path_for,
    row_artifact_path,
    safe_name_en,
)


def test_safe_name_strips_treatments_and_path_characters():
    assert safe_name_en("Fire // Ice (Borderless)") == "Fire -- Ice"


@pytest.mark.parametrize("filename", ["../outside.jpg", "nested/card.jpg"])
def test_confined_listing_path_rejects_non_child(filename, tmp_path):
    with pytest.raises(ValueError, match="listing filename escapes"):
        confined_listing_path(tmp_path, filename)


def test_row_artifact_path_accepts_supported_suffix(tmp_path):
    assert row_artifact_path("123_0", ".txt", tmp_path) == tmp_path / "123_0.txt"


def test_jpg_path_allows_current_generated_name(tmp_path):
    listing = {
        "row_id": "509556_0",
        "name_en": "Necropotence (Anime Borderless)",
        "printing": "Normal",
        "set_code": "WOT",
        "collector_number": "74",
    }
    current = tmp_path / "WOT-74 - Necropotence - 动漫无边框英文平.jpg"
    current.touch()

    assert jpg_path_for(listing, tmp_path, current_path=current) == current


def test_jpg_path_skips_reserved_dry_run_destination(tmp_path):
    listing = {
        "row_id": "509556_1",
        "name_en": "Necropotence (Anime Borderless)",
        "printing": "Normal",
        "set_code": "WOT",
        "collector_number": "74",
    }
    reserved = tmp_path / "WOT-74 - Necropotence - 动漫无边框英文平.jpg"

    path = jpg_path_for(listing, tmp_path, reserved_paths={reserved})

    assert path.name == "WOT-74 - Necropotence - 动漫无边框英文平 (copy 1).jpg"
