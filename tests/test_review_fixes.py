import json
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

    transport = _Transport({"prices": {"cny": "2.00"}})
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
        ui._impl,
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
    monkeypatch.setattr(ui._impl, "STATE_PATH", state_path)

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(ui._impl.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        ui._save_state({"new": True})

    assert json.loads(state_path.read_text(encoding="utf-8")) == {"old": True}
    assert list(tmp_path.iterdir()) == [state_path]
