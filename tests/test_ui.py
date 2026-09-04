import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from mtg_xianyu import ui
from mtg_xianyu.ui import (
    FX_RATE,
    _approval_hints,
    _build_unbound_pool,
    _build_listing,
    _filter_rows,
    _jpg_path_for,
    _next_unfinished_idx,
    _progress_counts,
    _resolve_final_price_and_source,
    _row_artifact_path,
    _safe_name_en,
    _validate_enriched_rows,
    effective_cny,
    sort_rows,
)


def _enriched_row(**overrides):
    row = {
        "row_id": "12345_0",
        "product_id": 12345,
        "name_en": "Lightning Bolt",
        "name_zh": "闪电击",
        "set_code": "M10",
        "set_name_en": "Magic 2010",
        "set_name_zh": "核心系列2010",
        "collector_number": "146",
        "condition": "Near Mint",
        "printing": "Normal",
        "rarity": "Common",
        "jihuanshe_price_cny": 12.5,
        "usd_market": 2.0,
        "sbwsz_image_uri": "https://example.test/card.jpg",
        "tcg_photo_url": "https://example.test/tcg.jpg",
    }
    return {**row, **overrides}


# ── persisted UI-data boundaries ──────────────────────────────────────────────

def test_validate_enriched_rows_accepts_canonical_data(tmp_path):
    rows = [_enriched_row()]
    assert _validate_enriched_rows(rows, tmp_path / "enriched.json") == rows


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"not": "a list"}, "expected a JSON array"),
        (["not an object"], "row 0 must be an object"),
        ([_enriched_row(row_id="")], "'row_id' must be a non-empty string"),
        ([_enriched_row(row_id="../outside")], "row_id.*generated"),
        ([_enriched_row(row_id="12345_00")], "row_id.*generated"),
        ([_enriched_row(row_id="999_0")], "row_id.*does not match product_id"),
        ([_enriched_row(usd_market="2.00")], "'usd_market'.*finite"),
    ],
)
def test_validate_enriched_rows_rejects_malformed_data(tmp_path, data, message):
    with pytest.raises(ValueError, match=message):
        _validate_enriched_rows(data, tmp_path / "enriched.json")


def test_validate_enriched_rows_rejects_duplicate_ids(tmp_path):
    rows = [_enriched_row(), _enriched_row(name_en="Counterspell")]
    with pytest.raises(ValueError, match="duplicate row_id '12345_0'"):
        _validate_enriched_rows(rows, tmp_path / "enriched.json")


def test_load_enriched_reports_invalid_json_with_path(tmp_path):
    path = tmp_path / "enriched.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match=r"cannot load enriched data .*enriched\.json"):
        ui._load_enriched(path)


def test_load_state_migrates_missing_legacy_fields_atomically(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "fx_rate": FX_RATE,
        "rows": {"12345_0": {
            "state": "ready_to_review",
            "photo_path": "data/mtg_photos/card.heic",
        }},
    }), encoding="utf-8")
    monkeypatch.setattr(ui, "STATE_PATH", path)

    state = ui._load_state()

    entry = state["rows"]["12345_0"]
    assert entry["photo_path"] == "data/mtg_photos/card.heic"
    assert entry["price_error"] is None
    assert entry["price_cny"] is None
    assert json.loads(path.read_text(encoding="utf-8")) == state
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"state": "mystery"}, "unknown state 'mystery'"),
        ({"state": ["ready_to_review"]}, "unknown state"),
        ({"state": "ready_to_review", "price_cny": -1}, "price_cny.*finite"),
        (
            {"state": "ready_to_review", "price_cny": 10, "price_source": ["jhs"]},
            "unknown price_source",
        ),
        (
            {"state": "ready_to_review", "price_cny": 10, "price_source": None},
            "price_cny but no price_source",
        ),
        ({"state": "approved"}, "approved row.*approved_at"),
    ],
)
def test_load_state_rejects_invalid_rows_without_overwriting(
    tmp_path, monkeypatch, entry, message
):
    path = tmp_path / "state.json"
    original = json.dumps({"fx_rate": FX_RATE, "rows": {"12345_0": entry}})
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(ui, "STATE_PATH", path)

    with pytest.raises(ValueError, match=message):
        ui._load_state()

    assert path.read_text(encoding="utf-8") == original


def test_load_state_reports_invalid_json_without_overwriting(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    path.write_text("not json", encoding="utf-8")
    monkeypatch.setattr(ui, "STATE_PATH", path)

    with pytest.raises(ValueError, match=r"cannot load UI state .*state\.json"):
        ui._load_state()

    assert path.read_text(encoding="utf-8") == "not json"


def test_progress_counts_ignore_stale_state_rows():
    rows = [_enriched_row(row_id="current_0"), _enriched_row(row_id="current_1")]
    state = {"rows": {
        "current_0": {"state": "approved"},
        "current_1": {"state": "waiting_photo"},
        "stale_0": {"state": "approved"},
        "stale_1": {"state": "skipped"},
    }}
    assert _progress_counts(rows, state) == (1, 0, 1)


def test_unbound_pool_ignores_stale_state_bindings(tmp_path, monkeypatch):
    current_photo = tmp_path / "current.heic"
    stale_photo = tmp_path / "stale.heic"
    monkeypatch.setattr(ui, "_scan_photos", lambda: [current_photo, stale_photo])
    state = {"rows": {
        "current_0": {"photo_path": str(current_photo)},
        "stale_0": {"photo_path": str(stale_photo)},
    }}

    assert _build_unbound_pool(state, {"current_0"}) == [stale_photo]


# ── photo-pool identity ───────────────────────────────────────────────────────

def test_duplicate_photo_basenames_have_distinct_widget_keys(tmp_path):
    first = tmp_path / "batch-a" / "IMG_0001.HEIC"
    second = tmp_path / "batch-b" / "IMG_0001.HEIC"

    assert ui._photo_widget_key(first) == ui._photo_widget_key(first)
    assert ui._photo_widget_key(first) != ui._photo_widget_key(second)


def test_duplicate_photo_basenames_show_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ui, "PHOTO_DIR", tmp_path)
    first = tmp_path / "batch-a" / "IMG_0001.HEIC"
    second = tmp_path / "batch-b" / "IMG_0001.HEIC"

    assert ui._photo_display_label(first) == "batch-a/IMG_0001.HEIC"
    assert ui._photo_display_label(second) == "batch-b/IMG_0001.HEIC"


# ── approved-row immutability ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: ui._set_row_state(state, "12345_0", price_cny=99.0),
        lambda state: ui._bind_photo("12345_0", Path("replacement.heic"), state),
        lambda state: ui._unbind_photo("12345_0", state),
        lambda state: ui._do_approve({}, "12345_0", state),
    ],
)
def test_approved_rows_reject_direct_mutations(mutation):
    state = {"rows": {"12345_0": {"state": "approved"}}}
    original = deepcopy(state)

    with pytest.raises(ValueError, match="approved and read-only"):
        mutation(state)

    assert state == original


@pytest.mark.parametrize(
    "callback",
    [ui._on_name_change, ui._on_price_override_change],
)
def test_approved_rows_reject_widget_callbacks(monkeypatch, callback):
    state = {"rows": {"12345_0": {"state": "approved"}}}
    original = deepcopy(state)
    monkeypatch.setattr(
        ui,
        "st",
        SimpleNamespace(session_state=SimpleNamespace(state=state)),
    )

    with pytest.raises(ValueError, match="approved and read-only"):
        callback("12345_0")

    assert state == original


# ── effective_cny ─────────────────────────────────────────────────────────────

def test_effective_cny_uses_jhs_when_present():
    assert effective_cny({"jihuanshe_price_cny": 42.5, "usd_market": 10.0}) == 42.5


def test_effective_cny_falls_back_to_usd_when_jhs_null():
    result = effective_cny({"jihuanshe_price_cny": None, "usd_market": 10.0})
    assert abs(result - 10.0 * FX_RATE) < 0.001


def test_effective_cny_returns_zero_when_both_null():
    assert effective_cny({"jihuanshe_price_cny": None, "usd_market": None}) == 0.0


def test_effective_cny_jhs_zero_is_not_fallthrough():
    # JHS price of 0.0 is a real recorded price, not absent — must not fall back to USD
    assert effective_cny({"jihuanshe_price_cny": 0.0, "usd_market": 50.0}) == 0.0


def test_effective_cny_missing_keys():
    assert effective_cny({}) == 0.0


# ── sort_rows ─────────────────────────────────────────────────────────────────

def test_sort_rows_effective_cny_descending():
    rows = [
        {"jihuanshe_price_cny": 10.0, "usd_market": 1.0},
        {"jihuanshe_price_cny": 50.0, "usd_market": 1.0},
        {"jihuanshe_price_cny": 25.0, "usd_market": 1.0},
    ]
    result = sort_rows(rows, "Effective CNY")
    prices = [r["jihuanshe_price_cny"] for r in result]
    assert prices == [50.0, 25.0, 10.0]


def test_sort_rows_usd_descending():
    rows = [
        {"usd_market": 5.0,  "jihuanshe_price_cny": None},
        {"usd_market": 20.0, "jihuanshe_price_cny": None},
        {"usd_market": 1.0,  "jihuanshe_price_cny": None},
    ]
    result = sort_rows(rows, "USD market")
    usds = [r["usd_market"] for r in result]
    assert usds == [20.0, 5.0, 1.0]


def test_sort_rows_does_not_mutate_input():
    rows = [
        {"jihuanshe_price_cny": 10.0},
        {"jihuanshe_price_cny": 99.0},
    ]
    original_order = [r["jihuanshe_price_cny"] for r in rows]
    sort_rows(rows, "Effective CNY")
    assert [r["jihuanshe_price_cny"] for r in rows] == original_order


# ── _approval_hints ───────────────────────────────────────────────────────────

def test_approval_hint_blocking_no_price_no_default():
    # No JHS, no USD, no explicit price → price warning only.
    row = {"name_zh": "御用密令", "jihuanshe_price_cny": None, "usd_market": None}
    entry = {"price_cny": None, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    msg, level = hints[0]
    assert level == "warning"
    assert "price" in msg.lower()


def test_approval_hint_blocking_no_name():
    # name_zh null, no override → name warning only.
    row = {"name_zh": None, "jihuanshe_price_cny": None, "usd_market": None}
    entry = {"price_cny": 15.0, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    msg, level = hints[0]
    assert level == "warning"
    assert "chinese name" in msg.lower()


def test_approval_hint_default_jhs():
    # JHS available, price_cny not yet set → info hint citing the JHS value.
    row = {"name_zh": "御用密令", "jihuanshe_price_cny": 25.50, "usd_market": 10.0}
    entry = {"price_cny": None, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    msg, level = hints[0]
    assert level == "info"
    assert "25.50" in msg
    assert "JHS" in msg


def test_approval_hint_default_usd_only():
    # No JHS, USD present, price_cny not set → info hint citing USD-converted value.
    usd = 10.0
    row = {"name_zh": "御用密令", "jihuanshe_price_cny": None, "usd_market": usd}
    entry = {"price_cny": None, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    msg, level = hints[0]
    assert level == "info"
    expected = f"{usd * FX_RATE:.2f}"
    assert expected in msg
    assert "USD" in msg


def test_approval_hint_explicit_price():
    # User set price_cny, name_zh present → ready to approve.
    row = {"name_zh": "御用密令", "jihuanshe_price_cny": 25.0, "usd_market": 10.0}
    entry = {"price_cny": 22.0, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    _, level = hints[0]
    assert level == "success"


def test_approval_hint_override_name():
    # name_zh null but name_zh_override set → name condition satisfied.
    row = {"name_zh": None, "jihuanshe_price_cny": None, "usd_market": None}
    entry = {"price_cny": 15.0, "name_zh_override": "御用密令"}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    _, level = hints[0]
    assert level == "success"


def test_approval_hint_explicit_zero_price():
    # price_cny=0.0 is a valid explicit choice (user typed 0), not absent.
    row = {"name_zh": "御用密令", "jihuanshe_price_cny": None, "usd_market": None}
    entry = {"price_cny": 0.0, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    assert len(hints) == 1
    _, level = hints[0]
    assert level == "success"


def test_approval_hint_all_blocking():
    # Both price and name missing → both warnings surfaced, not just the first.
    row = {"name_zh": None, "jihuanshe_price_cny": None, "usd_market": None}
    entry = {"price_cny": None, "name_zh_override": None}
    hints = _approval_hints(row, entry)
    levels = [level for _, level in hints]
    messages = [msg for msg, _ in hints]
    assert levels.count("warning") == 2
    assert any("price" in m.lower() for m in messages)
    assert any("chinese name" in m.lower() for m in messages)


# ── _resolve_final_price_and_source ──────────────────────────────────────────

def test_price_source_inference_explicit_manual():
    row = {"jihuanshe_price_cny": 100.0, "usd_market": 20.0}
    state_row = {"price_cny": 88.0, "price_source": "manual"}
    price, source = _resolve_final_price_and_source(row, state_row, FX_RATE)
    assert price == 88.0
    assert source == "manual"


def test_price_source_inference_default_jhs():
    # No explicit choice → falls back to JHS when available.
    row = {"jihuanshe_price_cny": 100.0, "usd_market": 20.0}
    state_row = {"price_cny": None, "price_source": None}
    price, source = _resolve_final_price_and_source(row, state_row, FX_RATE)
    assert price == 100.0
    assert source == "jhs"


def test_price_source_inference_default_usd():
    # No JHS, no explicit choice → USD-converted.
    row = {"jihuanshe_price_cny": None, "usd_market": 10.0}
    state_row = {"price_cny": None, "price_source": None}
    price, source = _resolve_final_price_and_source(row, state_row, FX_RATE)
    assert abs(price - round(10.0 * FX_RATE, 2)) < 0.001
    assert source == "usd_converted"


# ── _build_listing ────────────────────────────────────────────────────────────

def test_approve_listing_shape():
    row = {
        "row_id": "534045_0",
        "product_id": 534045,
        "name_en": "Imperial Seal (Borderless)",
        "name_zh": "玉玺",
        "set_code": "2X2",
        "set_name_en": "Double Masters 2022",
        "set_name_zh": "双星大师2022",
        "collector_number": "354",
        "condition": "Near Mint",
        "printing": "Normal",
        "rarity": "Mythic",
        "jihuanshe_price_cny": 1155.91,
        "usd_market": 167.87,
    }
    state_row = {
        "photo_path": "data/mtg_photos/IMG_1234.HEIC",
        "price_cny": 1155.91,
        "price_source": "jhs",
        "name_zh_override": None,
    }
    ts = "2026-01-15T10:30:00"
    listing = _build_listing(row, state_row, 7.25, ts)

    assert listing["row_id"] == "534045_0"
    assert listing["product_id"] == 534045
    assert listing["name_en"] == "Imperial Seal (Borderless)"
    assert listing["name_zh"] == "玉玺"
    assert listing["set_code"] == "2X2"
    assert listing["collector_number"] == "354"
    assert listing["condition"] == "Near Mint"
    assert listing["printing"] == "Normal"
    assert listing["price_cny"] == 1155.91
    assert listing["price_source"] == "jhs"
    assert listing["fx_rate_at_approval"] == 7.25
    assert listing["photo_jpg"] == "data/listings/534045_0.jpg"
    assert listing["photo_heic_source"] == "data/mtg_photos/IMG_1234.HEIC"
    assert listing["approved_at"] == ts
    # All spec-required keys present
    required = {
        "row_id", "product_id", "name_en", "name_zh", "set_code",
        "set_name_en", "set_name_zh", "collector_number", "condition",
        "printing", "rarity", "jihuanshe_price_cny", "usd_market",
        "price_cny", "price_source", "fx_rate_at_approval",
        "photo_jpg", "photo_heic_source", "approved_at",
    }
    assert required.issubset(listing.keys())


def test_approve_listing_uses_name_override():
    row = {"row_id": "r0", "name_zh": "原始名称", "jihuanshe_price_cny": 50.0,
           "usd_market": None}
    state_row = {"photo_path": "p.heic", "price_cny": 50.0, "price_source": "jhs",
                 "name_zh_override": "覆盖名称"}
    listing = _build_listing(row, state_row, FX_RATE, "2026-01-01T00:00:00")
    assert listing["name_zh"] == "覆盖名称"


# ── _next_unfinished_idx ──────────────────────────────────────────────────────

def test_next_unfinished_row():
    rows = [
        {"row_id": "r0"},
        {"row_id": "r1"},
        {"row_id": "r2"},
        {"row_id": "r3"},
    ]
    state = {"rows": {
        "r0": {"state": "approved"},
        "r1": {"state": "ready_to_review"},
        "r2": {"state": "approved"},
        "r3": {"state": "ready_to_review"},
    }}
    assert _next_unfinished_idx(rows, state, 0, "All rows") == 1
    assert _next_unfinished_idx(rows, state, 1, "All rows") == 3
    assert _next_unfinished_idx(rows, state, 3, "All rows") == 1


def test_next_unfinished_row_returns_none_when_all_approved():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}]
    state = {"rows": {"r0": {"state": "approved"}, "r1": {"state": "approved"}}}
    assert _next_unfinished_idx(rows, state, 0, "All rows") is None


def test_next_unfinished_wraps_and_reindexes_filtered_view():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}, {"row_id": "r2"}]
    state = {"rows": {
        "r0": {"state": "ready_to_review"},
        "r1": {"state": "waiting_photo"},
        "r2": {"state": "approved"},
    }}

    assert _next_unfinished_idx(rows, state, 2, "Remaining only") == 0


def test_next_unfinished_rejects_unknown_view():
    with pytest.raises(ValueError, match="unknown review view"):
        _next_unfinished_idx([], {"rows": {}}, 0, "mystery")


def test_sort_rows_jhs_beats_usd_fallback():
    # A row with JHS ¥30 should rank above a row with USD $10 × 7.25 = ¥72.5
    # when the JHS row's price is higher
    rows = [
        {"jihuanshe_price_cny": None, "usd_market": 10.0},   # effective = 72.5
        {"jihuanshe_price_cny": 100.0, "usd_market": 1.0},   # effective = 100.0
    ]
    result = sort_rows(rows, "Effective CNY")
    assert result[0]["jihuanshe_price_cny"] == 100.0


# ── _safe_name_en / _jpg_path_for ────────────────────────────────────────────

def test_filename_generation_basic_card(tmp_path):
    listing = {
        "row_id": "234275_0",
        "name_en": "Urborg, Tomb of Yawgmoth",
        "printing": "Normal",
        "set_code": "TSR",
        "collector_number": "113",
    }
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert path.name == "TSR-113 - Urborg, Tomb of Yawgmoth - 英文平.jpg"


def test_filename_strips_parentheticals_from_name_en(tmp_path):
    listing = {
        "row_id": "276329_0",
        "name_en": "Imperial Seal (Borderless)",
        "printing": "Normal",
        "set_code": "2X2",
        "collector_number": "354",
    }
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert path.name == "2X2-354 - Imperial Seal - 异画英文平.jpg"
    assert "Borderless" not in path.name


def test_filename_includes_finish_zh(tmp_path):
    listing = {
        "row_id": "265346_0",
        "name_en": "Jetmir's Garden",
        "printing": "Foil",
        "set_code": "SNC",
        "collector_number": "250",
    }
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert "英文闪" in path.name
    assert path.name == "SNC-250 - Jetmir's Garden - 英文闪.jpg"


def test_filename_handles_slash_in_name(tmp_path):
    listing = {
        "row_id": "test_0",
        "name_en": "Fire // Ice",
        "printing": "Foil",
        "set_code": "USG",
        "collector_number": "100",
    }
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert "/" not in path.name
    assert path.name.startswith("USG-100 - Fire -- Ice")


def test_filename_slash_in_collector_number(tmp_path):
    # PLST collector numbers are "NNN/NNN" — the slash must not create a subdirectory
    listing = {
        "row_id": "222540_0",
        "name_en": "Temporal Manipulation",
        "printing": "Normal",
        "set_code": "PLST",
        "collector_number": "077/254",
    }
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert path.parent == tmp_path, "slash in collector_number must not create a subdirectory"
    assert path.name == "PLST-077-254 - Temporal Manipulation - 英文平.jpg"


def test_filename_collision_handling(tmp_path):
    listing = {
        "row_id": "276329_1",
        "name_en": "Imperial Seal (Borderless)",
        "printing": "Normal",
        "set_code": "2X2",
        "collector_number": "354",
    }
    (tmp_path / "2X2-354 - Imperial Seal - 异画英文平.jpg").touch()
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert path.name == "2X2-354 - Imperial Seal - 异画英文平 (copy 1).jpg"


def test_filename_collision_handling_finds_next_available_copy(tmp_path):
    listing = {
        "row_id": "276329_0",
        "name_en": "Imperial Seal (Borderless)",
        "printing": "Normal",
        "set_code": "2X2",
        "collector_number": "354",
    }
    stem = "2X2-354 - Imperial Seal - 异画英文平"
    (tmp_path / f"{stem}.jpg").touch()
    (tmp_path / f"{stem} (copy 1).jpg").touch()

    path = _jpg_path_for(listing, listings_dir=tmp_path)

    assert path.name == f"{stem} (copy 2).jpg"


def test_filename_reconciliation_allows_current_desired_path(tmp_path):
    listing = {
        "row_id": "276329_0",
        "name_en": "Imperial Seal (Borderless)",
        "printing": "Normal",
        "set_code": "2X2",
        "collector_number": "354",
    }
    current = tmp_path / "2X2-354 - Imperial Seal - 异画英文平.jpg"
    current.touch()

    assert _jpg_path_for(
        listing,
        listings_dir=tmp_path,
        current_path=current,
    ) == current


@pytest.mark.parametrize(
    ("field", "unsafe_value"),
    [
        ("set_code", "../outside"),
        ("collector_number", "../../outside"),
        ("name_en", "..\\outside\ncard"),
    ],
)
def test_filename_components_cannot_escape_listings_dir(
    tmp_path, field, unsafe_value
):
    listing = {
        "row_id": "123_0",
        "name_en": "Test Card",
        "printing": "Normal",
        "set_code": "TST",
        "collector_number": "1",
    }
    listing[field] = unsafe_value

    path = _jpg_path_for(listing, listings_dir=tmp_path)

    assert path.resolve().parent == tmp_path.resolve()
    assert "/" not in path.name
    assert "\\" not in path.name
    assert "\n" not in path.name


@pytest.mark.parametrize(
    "filename",
    ["../outside.jpg", "nested/../inside.jpg", "/tmp/outside.jpg"],
)
def test_listing_path_guard_rejects_non_child_paths(tmp_path, filename):
    with pytest.raises(ValueError, match="listing filename escapes"):
        ui._confined_listing_path(tmp_path, filename)


@pytest.mark.parametrize("suffix", [".json", ".txt"])
def test_row_artifact_paths_cannot_escape_listings_dir(tmp_path, suffix):
    with pytest.raises(ValueError, match="listing filename escapes"):
        _row_artifact_path("../outside", suffix, listings_dir=tmp_path)


def test_row_artifact_path_accepts_canonical_id(tmp_path):
    path = _row_artifact_path("12345_0", ".json", listings_dir=tmp_path)
    assert path == tmp_path / "12345_0.json"


def test_filename_fallback_for_degenerate_data(tmp_path):
    listing = {
        "row_id": "bad_0",
        "name_en": "(Borderless)",   # strips to empty string
        "printing": "Normal",
        "set_code": "TST",
        "collector_number": "1",
    }
    path = _jpg_path_for(listing, listings_dir=tmp_path)
    assert path.name == "bad_0.jpg"


def test_listing_json_photo_jpg_points_at_new_name(tmp_path):
    listing = {
        "row_id": "276329_0",
        "name_en": "Imperial Seal (Borderless)",
        "printing": "Normal",
        "set_code": "2X2",
        "collector_number": "354",
        "photo_jpg": "data/listings/276329_0.jpg",   # old opaque name
    }
    listing["photo_jpg"] = str(_jpg_path_for(listing, listings_dir=tmp_path))
    assert "Imperial Seal" in listing["photo_jpg"]
    assert "2X2-354" in listing["photo_jpg"]
    assert "276329_0.jpg" not in listing["photo_jpg"]


# ── _filter_rows ──────────────────────────────────────────────────────────────

def _make_state(*pairs):
    """Build a minimal state dict from (row_id, state_str) pairs."""
    return {"rows": {rid: {"state": s} for rid, s in pairs}}


def test_filter_default_view_includes_ready_and_approved():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}]
    state = _make_state(("r0", "ready_to_review"), ("r1", "approved"))
    assert len(_filter_rows(rows, state, "Default")) == 2


def test_filter_default_excludes_waiting_and_skipped():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}, {"row_id": "r2"}, {"row_id": "r3"}]
    state = _make_state(
        ("r0", "waiting_photo"), ("r1", "ready_to_review"),
        ("r2", "approved"),      ("r3", "skipped"),
    )
    result = _filter_rows(rows, state, "Default")
    assert [r["row_id"] for r in result] == ["r1", "r2"]


def test_filter_all_rows_includes_everything():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}, {"row_id": "r2"}, {"row_id": "r3"}]
    state = _make_state(
        ("r0", "waiting_photo"), ("r1", "ready_to_review"),
        ("r2", "approved"),      ("r3", "skipped"),
    )
    assert len(_filter_rows(rows, state, "All rows")) == 4


def test_filter_remaining_only_includes_waiting_and_ready():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}]
    state = _make_state(("r0", "waiting_photo"), ("r1", "ready_to_review"))
    assert len(_filter_rows(rows, state, "Remaining only")) == 2


def test_filter_remaining_only_excludes_approved_and_skipped():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}, {"row_id": "r2"}, {"row_id": "r3"}]
    state = _make_state(
        ("r0", "waiting_photo"), ("r1", "ready_to_review"),
        ("r2", "approved"),      ("r3", "skipped"),
    )
    result = _filter_rows(rows, state, "Remaining only")
    assert [r["row_id"] for r in result] == ["r0", "r1"]


def test_filter_skipped_only_only_skipped():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}, {"row_id": "r2"}]
    state = _make_state(("r0", "approved"), ("r1", "skipped"), ("r2", "waiting_photo"))
    result = _filter_rows(rows, state, "Skipped only")
    assert len(result) == 1
    assert result[0]["row_id"] == "r1"


def test_filter_empty_when_no_matching_states():
    rows = [{"row_id": "r0"}, {"row_id": "r1"}]
    state = _make_state(("r0", "approved"), ("r1", "approved"))
    assert _filter_rows(rows, state, "Skipped only") == []
