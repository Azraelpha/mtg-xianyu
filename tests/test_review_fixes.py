import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from mtg_xianyu import enrich, ui


class _Transport(httpx.BaseTransport):
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json=self.payload)


class _RoutesTransport(httpx.BaseTransport):
    def __init__(self, routes):
        self.routes = routes

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url not in self.routes:
            raise AssertionError(f"Unexpected network request: {url}")
        return httpx.Response(200, json=self.routes[url])


def _search_url(name: str) -> str:
    return str(httpx.URL(f"{enrich.BASE_URL}/result", params={
        "q": f'name:"{name}"',
        "priority_chinese": "true",
        "unique": "oracle_id",
        "view": "0",
        "page_size": "50",
    }))


def _search_result(name: str, name_zh: str) -> dict:
    return {"items": [{
        "name": name,
        "atomic_official_name": name_zh,
        "atomic_translated_name": None,
        "set": "M10",
        "collector_number": "146",
    }]}


def test_legacy_card_cache_is_refreshed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        enrich,
        "_cache_path",
        lambda kind, *parts: tmp_path / kind / ("_".join(parts) + ".json"),
    )
    url = f"{enrich.BASE_URL}/card/MH3/1/?view=1"
    path = enrich._url_cache_path("cards", url, "MH3", "1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"prices": {"cny": "1.00"}}', encoding="utf-8")

    transport = _Transport({
        "prices": {"cny": "2.00"},
        "faces": [{"name": "Test Card"}],
    })
    client = httpx.Client(transport=transport)

    first = enrich._get_card(client, "MH3", "1")
    second = enrich._get_card(client, "MH3", "1")

    assert first["prices"]["cny"] == "2.00"
    assert second["prices"]["cny"] == "2.00"
    assert transport.calls == 1
    assert '"_cache_kind": "card_response_v1"' in path.read_text(encoding="utf-8")


def test_card_cache_identity_includes_query_string(tmp_path, monkeypatch):
    monkeypatch.setattr(
        enrich,
        "_cache_path",
        lambda kind, *parts: tmp_path / kind / ("_".join(parts) + ".json"),
    )
    bare = enrich._url_cache_path("cards", "https://example.test/card/1", "SET", "1")
    viewed = enrich._url_cache_path(
        "cards", "https://example.test/card/1?view=1", "SET", "1"
    )
    assert bare != viewed


@pytest.mark.parametrize(
    "component",
    ["../outside", "/tmp/outside", "nested/path", r"nested\path", ".", ".."],
)
def test_card_cache_display_parts_cannot_escape_cache_root(component):
    path = enrich._url_cache_path(
        component,
        f"https://example.test/card?q={component}",
        component,
        component,
    )
    root = Path("data/cache/sbwsz").resolve()

    assert path.resolve().is_relative_to(root)
    assert path.name.endswith(".json")
    assert component not in path.parts


def test_card_cache_display_parts_are_bounded_without_losing_url_identity():
    long_component = "x" * 500
    first = enrich._url_cache_path(
        "cards", "https://example.test/card?view=1", long_component
    )
    second = enrich._url_cache_path(
        "cards", "https://example.test/card?view=2", long_component
    )

    assert len(first.name.encode("utf-8")) <= 100
    assert first != second


def test_missing_face_identity_fails_closed_and_borrows_only_name(tmp_path, monkeypatch):
    monkeypatch.setattr(
        enrich,
        "_cache_path",
        lambda kind, *parts: tmp_path / kind / ("_".join(parts) + ".json"),
    )
    card_url = f"{enrich.BASE_URL}/card/MH3/146/?view=1"
    routes = {
        card_url: {
            "primary_name": "错误名称",
            "faces": [{"image_uris": {"normal": "https://example.test/wrong.jpg"}}],
            "prices": {"cny": "99.00"},
        },
        _search_url("Lightning Bolt"): _search_result("Lightning Bolt", "闪电击"),
    }
    client = httpx.Client(transport=_RoutesTransport(routes))
    row = {
        "set_name_en": "Modern Horizons 3",
        "name_en": "Lightning Bolt",
        "collector_number": "146",
    }
    result = enrich._enrich_row(
        0,
        row,
        {"modern horizons 3": "MH3"},
        {"MH3": "现代地平线3"},
        client,
    )
    assert result["name_zh"] == "闪电击"
    assert result["jihuanshe_price_cny"] is None
    assert result["sbwsz_image_uri"] is None


def test_missing_primary_name_borrows_name_but_keeps_printing_data(tmp_path, monkeypatch):
    monkeypatch.setattr(
        enrich,
        "_cache_path",
        lambda kind, *parts: tmp_path / kind / ("_".join(parts) + ".json"),
    )
    english_image = "https://example.test/english.jpg"
    card_url = f"{enrich.BASE_URL}/card/MH3/146/?view=1"
    routes = {
        card_url: {
            "primary_name": None,
            "translation_info": None,
            "faces": [{
                "name": "Lightning Bolt",
                "image_uris": {"normal": english_image},
                "zhs_image_uris": {"normal": "https://example.test/chinese.jpg"},
            }],
            "prices": {"cny": "12.50"},
        },
        _search_url("Lightning Bolt"): _search_result("Lightning Bolt", "闪电击"),
    }
    client = httpx.Client(transport=_RoutesTransport(routes))
    row = {
        "set_name_en": "Modern Horizons 3",
        "name_en": "Lightning Bolt",
        "collector_number": "146",
    }
    result = enrich._enrich_row(
        0,
        row,
        {"modern horizons 3": "MH3"},
        {"MH3": "现代地平线3"},
        client,
    )
    assert result["name_zh"] == "闪电击"
    assert result["jihuanshe_price_cny"] == 12.5
    assert result["sbwsz_image_uri"] == english_image


def test_known_score_86_fuzzy_set_match_is_rejected():
    en_to_code = {
        "teenage mutant ninja turtles eternal front cards": "TMT",
    }
    with pytest.raises(ValueError, match="no set_code"):
        enrich._resolve_set_code(
            "MagicFest Cards",
            en_to_code,
            "test row",
            code_to_name={"TMT": "Teenage Mutant Ninja Turtles Eternal Front Cards"},
        )


def test_auto_advance_reindexes_after_current_row_leaves_filtered_view(monkeypatch):
    rows = [{"row_id": "a"}, {"row_id": "b"}, {"row_id": "c"}]
    state = {"rows": {
        "a": {"state": "approved"},
        "b": {"state": "ready_to_review"},
        "c": {"state": "ready_to_review"},
    }}
    monkeypatch.setattr(
        ui,
        "st",
        SimpleNamespace(session_state={"_applied_view": "Remaining only"}),
    )

    # b is the next logical row, and after removing approved a it is index 0.
    assert ui._next_unfinished_idx(rows, state, 0) == 0


def test_unresolved_set_blocks_approval():
    row = {
        "name_zh": "测试牌",
        "set_code": None,
        "jihuanshe_price_cny": 10.0,
        "usd_market": 2.0,
    }
    entry = {"price_cny": 10.0, "price_source": "jhs", "name_zh_override": None}
    hints = ui._approval_hints(row, entry)
    assert any(level == "warning" and "Set identity" in msg for msg, level in hints)
    assert not any(level == "success" for _, level in hints)


def test_missing_bound_photo_blocks_approval(tmp_path):
    row = {
        "name_zh": "测试牌",
        "set_code": "TST",
        "jihuanshe_price_cny": 10.0,
        "usd_market": 2.0,
    }
    entry = {
        "photo_path": str(tmp_path / "missing.heic"),
        "price_cny": 10.0,
        "price_source": "jhs",
        "name_zh_override": None,
    }
    hints = ui._approval_hints(row, entry)
    assert any(level == "warning" and "photo" in msg.lower() for msg, level in hints)


def test_unreadable_bound_photo_blocks_approval(tmp_path):
    photo = tmp_path / "broken.jpg"
    photo.write_text("not an image", encoding="utf-8")
    row = {
        "name_zh": "测试牌",
        "set_code": "TST",
        "jihuanshe_price_cny": 10.0,
        "usd_market": 2.0,
    }
    entry = {
        "photo_path": str(photo),
        "price_cny": 10.0,
        "price_source": "jhs",
        "name_zh_override": None,
    }
    hints = ui._approval_hints(row, entry)
    assert any(level == "warning" and "unreadable" in msg.lower() for msg, level in hints)


def test_readable_bound_photo_allows_approval(tmp_path):
    photo = tmp_path / "card.jpg"
    Image.new("RGB", (2, 2), "white").save(photo)
    row = {
        "name_zh": "测试牌",
        "set_code": "TST",
        "jihuanshe_price_cny": 10.0,
        "usd_market": 2.0,
    }
    entry = {
        "photo_path": str(photo),
        "price_cny": 10.0,
        "price_source": "jhs",
        "name_zh_override": None,
    }
    assert ui._approval_hints(row, entry) == [("✓ Ready to approve", "success")]


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "not-a-number"])
def test_manual_price_rejects_unsafe_values(value):
    with pytest.raises(ValueError):
        ui._parse_manual_price(value)


def test_state_write_is_atomic_on_replace_failure(tmp_path, monkeypatch):
    state_path = tmp_path / "state.json"
    state_path.write_text('{"old": true}', encoding="utf-8")
    monkeypatch.setattr(ui, "STATE_PATH", state_path)

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ui.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        ui._save_state({"new": True})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.iterdir()) == [state_path]


def _approval_fixture(tmp_path):
    photo = tmp_path / "source.png"
    Image.new("RGB", (4, 4), "blue").save(photo)
    row = {
        "row_id": "1_0",
        "product_id": 1,
        "name_en": "Test Card",
        "name_zh": "测试牌",
        "set_code": "TST",
        "set_name_en": "Test Set",
        "set_name_zh": "测试系列",
        "collector_number": "1",
        "condition": "Near Mint",
        "printing": "Normal",
        "rarity": "Common",
        "jihuanshe_price_cny": 10.0,
        "usd_market": 2.0,
    }
    state = {"rows": {"1_0": {
        "state": "ready_to_review",
        "photo_path": str(photo),
        "name_zh_override": None,
        "price_cny": 10.0,
        "price_source": "jhs",
        "price_error": None,
        "approved_at": None,
    }}}
    return row, state


def test_approval_installs_complete_artifact_set_and_persists_state(
    tmp_path, monkeypatch
):
    row, state = _approval_fixture(tmp_path)
    listings = tmp_path / "listings"
    monkeypatch.setattr(ui, "LISTINGS_DIR", listings)
    monkeypatch.setattr(ui, "STATE_PATH", listings / "state.json")

    ui._do_approve(row, "1_0", state)

    listing = json.loads((listings / "1_0.json").read_text(encoding="utf-8"))
    assert Path(listing["photo_jpg"]).is_file()
    assert (listings / "1_0.txt").read_text(encoding="utf-8")
    assert state["rows"]["1_0"]["state"] == "approved"
    persisted = json.loads((listings / "state.json").read_text(encoding="utf-8"))
    assert persisted["rows"]["1_0"]["state"] == "approved"
    assert not any(path.name.startswith(".") for path in listings.iterdir())


def test_approval_artifact_install_rolls_back_partial_replace(tmp_path, monkeypatch):
    row, state = _approval_fixture(tmp_path)
    listings = tmp_path / "listings"
    listings.mkdir()
    monkeypatch.setattr(ui, "LISTINGS_DIR", listings)
    monkeypatch.setattr(ui, "STATE_PATH", listings / "state.json")
    real_replace = ui.os.replace

    def fail_json_replace(source, destination):
        if str(destination).endswith("1_0.json"):
            raise OSError("simulated artifact replace failure")
        real_replace(source, destination)

    monkeypatch.setattr(ui.os, "replace", fail_json_replace)
    with pytest.raises(OSError, match="artifact replace"):
        ui._do_approve(row, "1_0", state)

    assert state["rows"]["1_0"]["state"] == "ready_to_review"
    assert list(listings.iterdir()) == []


def test_approval_rolls_back_artifacts_when_state_save_fails(tmp_path, monkeypatch):
    row, state = _approval_fixture(tmp_path)
    listings = tmp_path / "listings"
    listings.mkdir()
    monkeypatch.setattr(ui, "LISTINGS_DIR", listings)
    monkeypatch.setattr(ui, "STATE_PATH", listings / "state.json")

    def fail_state_save(_state):
        raise OSError("simulated state failure")

    monkeypatch.setattr(ui, "_save_state", fail_state_save)
    with pytest.raises(OSError, match="state failure"):
        ui._do_approve(row, "1_0", state)

    assert state["rows"]["1_0"]["state"] == "ready_to_review"
    assert list(listings.iterdir()) == []
