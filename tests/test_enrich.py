import json
import time
from pathlib import Path

import httpx
import pytest

from mtg_xianyu import enrich
from mtg_xianyu.enrich import (
    BASE_URL,
    _assign_row_ids,
    _build_count_to_codes,
    _build_set_dicts,
    _enrich_row,
    _jihuanshe_price,
    _name_zh,
    _normalize_for_compare,
    _resolve_plst_slash,
    _resolve_set_code,
    _search_card_by_name,
    _strip_parenthetical,
)

# ── test data ──────────────────────────────────────────────────────────────────

SETS_DATA = [
    {"code": "MH3", "name": "Modern Horizons 3", "translated_name": "现代地平线3"},
    {"code": "SLD", "name": "Secret Lair Drop", "translated_name": "秘密巢穴"},
    {"code": "M3C", "name": "Modern Horizons 3 Commander", "translated_name": "摩登新篇3统帅"},
    {"code": "FIN", "name": "Final Fantasy", "translated_name": "最终幻想"},
    {"code": "PLST", "name": "The List", "translated_name": "系列：精选", "card_count": 300},
    {"code": "UMA", "name": "Ultimate Masters", "translated_name": "终极大师", "card_count": 254},
]


def _card(**overrides) -> dict:
    base = {
        "primary_name": "闪电击",
        "translation_info": {"name_source": "官方中文"},
        "faces": [
            {
                "name": "Lightning Bolt",
                "image_uris": {"normal": "https://example.com/img.jpg"},
                "zhs_image_uris": {"normal": "https://example.com/zhs.jpg"},
            }
        ],
        "prices": {"cny": "12.50"},
    }
    return {**base, **overrides}


def _row(**overrides) -> dict:
    base = {
        "product_id": 12345,
        "set_name_en": "Modern Horizons 3",
        "name_en": "Lightning Bolt",
        "collector_number": "146",
        "rarity": "Uncommon",
        "condition": "Near Mint",
        "printing": "Normal",
        "usd_market": 1.5,
        "tcg_photo_url": "https://example.com/1.jpg",
    }
    return {**base, **overrides}


# ── mock transport ─────────────────────────────────────────────────────────────

class _MockTransport(httpx.BaseTransport):
    def __init__(self, routes: dict[str, object]) -> None:
        self._routes = routes
        self.calls: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url not in self._routes:
            raise AssertionError(f"Unexpected network request: {url}")
        return httpx.Response(200, json=self._routes[url])


def _make_client(routes: dict) -> tuple[httpx.Client, _MockTransport]:
    transport = _MockTransport(routes)
    client = httpx.Client(transport=transport, headers={"User-Agent": "test"})
    return client, transport


# ── fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_cache(tmp_path, monkeypatch):
    """Redirect all cache I/O to a throwaway temp directory."""
    def patched(kind: str, *parts: str) -> Path:
        base = tmp_path / "cache"
        if parts:
            return base / kind / Path(*parts).with_suffix(".json")
        return base / f"{kind}.json"
    monkeypatch.setattr(enrich, "_cache_path", patched)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    enrich._last_request_at = 0.0
    yield
    enrich._last_request_at = 0.0


# ── disk and API boundaries ───────────────────────────────────────────────────

def test_load_rows_accepts_canonical_parse_output(tmp_path):
    path = tmp_path / "rows.json"
    expected = [_row()]
    path.write_text(json.dumps(expected), encoding="utf-8")

    assert enrich._load_rows(path) == expected


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"not": "a list"}, "expected a JSON array"),
        (["not an object"], "row 0: expected an object"),
        ([{**_row(), "set_name_en": ""}], "'set_name_en'.*input row 0"),
        ([{**_row(), "product_id": 0}], "'product_id'.*input row 0"),
        ([{**_row(), "usd_market": "1.50"}], "'usd_market'.*input row 0"),
    ],
)
def test_load_rows_rejects_malformed_shapes_with_row_context(
    tmp_path, payload, message
):
    path = tmp_path / "rows.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        enrich._load_rows(path)


def test_load_rows_reports_invalid_json_with_path(tmp_path):
    path = tmp_path / "rows.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(ValueError, match=r"cannot load enrichment input .*rows\.json"):
        enrich._load_rows(path)


@pytest.mark.parametrize(
    "cached",
    [
        ["legacy wrong shape"],
        {"fetched_at": "recent", "data": SETS_DATA},
        {"fetched_at": time.time(), "data": {"not": "a list"}},
    ],
)
def test_malformed_sets_cache_is_refetched(cached):
    path = enrich._cache_path("sets")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cached), encoding="utf-8")
    url = f"{BASE_URL}/sets/"
    client, transport = _make_client({url: SETS_DATA})

    assert enrich._get_sets(client) == SETS_DATA
    assert transport.calls == [url]


def test_malformed_card_cache_is_refetched():
    url = f"{BASE_URL}/card/MH3/146/?view=1"
    path = enrich._url_cache_path("cards", url, "MH3", "146")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_cache_kind": "card_response_v1",
        "fetched_at": time.time(),
        "data": {"faces": "not a list"},
    }), encoding="utf-8")
    client, transport = _make_client({url: _card()})

    assert enrich._get_card(client, "MH3", "146")["primary_name"] == "闪电击"
    assert transport.calls == [url]


def test_malformed_live_sets_response_names_endpoint():
    url = f"{BASE_URL}/sets/"
    client, _ = _make_client({url: {"not": "a list"}})

    with pytest.raises(ValueError, match=r"sets response.*api/v1/sets/.*JSON array"):
        enrich._get_sets(client)
    assert not enrich._cache_path("sets").exists()


def test_malformed_live_card_response_names_endpoint():
    url = f"{BASE_URL}/card/MH3/146/?view=1"
    client, _ = _make_client({url: {"faces": "not a list"}})

    with pytest.raises(ValueError, match=r"card response.*MH3/146.*'faces'"):
        enrich._get_card(client, "MH3", "146")
    assert not enrich._url_cache_path("cards", url, "MH3", "146").exists()


def test_malformed_live_search_response_names_endpoint():
    url = _search_url("Lightning Bolt")
    client, _ = _make_client({url: {"items": {"not": "a list"}}})

    with pytest.raises(ValueError, match=r"search response.*api/v1/result.*'items'"):
        _search_card_by_name(client, "Lightning Bolt")
    assert not enrich._url_cache_path(
        "cards", url, "_name_search", "Lightning_Bolt"
    ).exists()


def test_non_json_live_response_names_endpoint():
    class _InvalidJsonTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

    client = httpx.Client(transport=_InvalidJsonTransport())
    with pytest.raises(ValueError, match=r"invalid JSON.*MH3/146"):
        enrich._get_card(client, "MH3", "146")


# ── set resolution ─────────────────────────────────────────────────────────────

def test_exact_set_match():
    en_to_code, _, code_to_name = _build_set_dicts(SETS_DATA)
    assert _resolve_set_code("Modern Horizons 3", en_to_code, "test") == "MH3"


def test_fuzzy_set_match(capsys):
    en_to_code, _, code_to_name = _build_set_dicts(SETS_DATA)
    code = _resolve_set_code("Modern Horizons III", en_to_code, "test")
    assert code == "MH3"
    assert "[fuzzy]" in capsys.readouterr().err


def test_unknown_set_raises():
    en_to_code, _, code_to_name = _build_set_dicts(SETS_DATA)
    with pytest.raises(ValueError, match="no set_code"):
        _resolve_set_code("Completely Unknown Set XYZ 9999", en_to_code, "row ref")


def test_case_insensitive_exact_match():
    en_to_code, _, code_to_name = _build_set_dicts(SETS_DATA)
    log: list = []
    code = _resolve_set_code("FINAL FANTASY", en_to_code, "test", fuzzy_log=log, code_to_name=code_to_name)
    assert code == "FIN"
    assert log == []  # exact match after casefold — no fuzzy entry


def test_fuzzy_handles_word_reorder(capsys):
    en_to_code, _, code_to_name = _build_set_dicts(SETS_DATA)
    log: list = []
    code = _resolve_set_code(
        "Commander: Modern Horizons 3", en_to_code, "test",
        fuzzy_log=log, code_to_name=code_to_name,
    )
    assert code == "M3C"
    assert len(log) == 1
    inp, match, score = log[0]
    assert inp == "Commander: Modern Horizons 3"   # original-case input
    assert match == "Modern Horizons 3 Commander"  # original-case sbwsz name
    assert score >= 80
    assert "[fuzzy]" in capsys.readouterr().err


# ── name_zh extraction ─────────────────────────────────────────────────────────

def test_name_zh_official():
    card = _card(primary_name="闪电击", translation_info={"name_source": "官方中文"})
    assert _name_zh(card) == "闪电击"


def test_name_zh_community_fallback(capsys):
    card = _card(primary_name="苍茂谷唤洪师", translation_info={"name_source": "MTGso"})
    assert _name_zh(card) == "苍茂谷唤洪师"
    assert "[name_zh]" in capsys.readouterr().err


def test_null_name_zh():
    card = _card(primary_name=None, translation_info=None)
    assert _name_zh(card) is None


# ── jihuanshe price extraction ─────────────────────────────────────────────────

def test_jihuanshe_price_extracted():
    assert _jihuanshe_price(_card(prices={"cny": "17.27"})) == pytest.approx(17.27)


def test_null_jihuanshe():
    assert _jihuanshe_price(_card(prices={})) is None


def test_jihuanshe_price_null_value():
    assert _jihuanshe_price(_card(prices={"cny": None})) is None


def test_jihuanshe_price_empty_string():
    assert _jihuanshe_price(_card(prices={"cny": ""})) is None


# ── 404 handling ──────────────────────────────────────────────────────────────

def test_404_returns_null_enrichment(capsys):
    routes = {"https://mtgch.com/api/v1/card/MH3/146/?view=1": _card()}
    transport = _MockTransport(routes)
    transport._routes = {}  # empty — every request 404s via AssertionError... use a real 404

    class _404Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

    client = httpx.Client(transport=_404Transport(), headers={"User-Agent": "test"})
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(SETS_DATA)
    result = _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert result["name_zh"] is None
    assert result["jihuanshe_price_cny"] is None
    assert "[404]" in capsys.readouterr().err


# ── network / cache behaviour ──────────────────────────────────────────────────

def test_404_card_lookup_is_cached_for_the_ttl():
    class _404Transport(httpx.BaseTransport):
        def __init__(self):
            self.calls = 0

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            return httpx.Response(404)

    transport = _404Transport()
    client = httpx.Client(transport=transport, headers={"User-Agent": "test"})
    url = f"{BASE_URL}/card/MH3/999/?view=1"
    path = enrich._url_cache_path("cards", url, "MH3", "999")

    assert enrich._get_card(client, "MH3", "999") == {}
    assert enrich._get_card(client, "MH3", "999") == {}

    assert transport.calls == 1
    cached = json.loads(path.read_text(encoding="utf-8"))
    assert cached["data"] == {"_not_found": True}


def test_expired_404_card_cache_is_refetched():
    url = f"{BASE_URL}/card/MH3/146/?view=1"
    path = enrich._url_cache_path("cards", url, "MH3", "146")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_cache_kind": "card_response_v1",
        "fetched_at": time.time() - enrich.CARD_CACHE_TTL - 1,
        "data": {"_not_found": True},
    }), encoding="utf-8")
    client, transport = _make_client({url: _card()})

    result = enrich._get_card(client, "MH3", "146")

    assert result["primary_name"] == "闪电击"
    assert transport.calls == [url]


def test_view_1_in_cached_url():
    routes = {"https://mtgch.com/api/v1/card/MH3/146/?view=1": _card()}
    client, transport = _make_client(routes)
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(SETS_DATA)
    _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert transport.calls == ["https://mtgch.com/api/v1/card/MH3/146/?view=1"]


def test_cache_hit_skips_network():
    routes = {"https://mtgch.com/api/v1/card/MH3/146/?view=1": _card()}
    client, transport = _make_client(routes)
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(SETS_DATA)

    _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert len(transport.calls) == 1

    _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert len(transport.calls) == 1  # cache hit — no second request


# ── PLST slash-format recovery ─────────────────────────────────────────────────

def _card_named(en_name: str, zh_name: str = "中文名") -> dict:
    """Card whose faces[0]["name"] is en_name."""
    return _card(
        primary_name=zh_name,
        faces=[{
            "name": en_name,
            "image_uris": {"normal": "https://example.com/img.jpg"},
            "zhs_image_uris": {"normal": "https://example.com/zhs.jpg"},
        }],
    )


def test_plst_slash_unique_total():
    # card_count=254 → only UMA; "077/254" → PLST/UMA-77
    count_to_codes = _build_count_to_codes(SETS_DATA)  # UMA: card_count=254
    routes = {"https://mtgch.com/api/v1/card/PLST/UMA-77/?view=1": _card_named("Temporal Manipulation", "操弄时间")}
    client, transport = _make_client(routes)
    result = _resolve_plst_slash(client, "077/254", "Temporal Manipulation", count_to_codes)
    assert result["primary_name"] == "操弄时间"
    assert transport.calls == ["https://mtgch.com/api/v1/card/PLST/UMA-77/?view=1"]


def test_plst_slash_collision_name_match():
    # card_count=249 → WRONG then RIGHT; wrong candidate probed first, rejected by name; right wins
    count_to_codes = {"249": ["WRONG", "RIGHT"]}  # explicit: WRONG comes first
    routes = {
        "https://mtgch.com/api/v1/card/PLST/WRONG-48/?view=1": _card_named("Drain Power", "魔力流失"),
        "https://mtgch.com/api/v1/card/PLST/RIGHT-48/?view=1": _card_named("Cryptic Command", "奥秘命令"),
    }
    client, transport = _make_client(routes)
    result = _resolve_plst_slash(client, "048/249", "Cryptic Command", count_to_codes)
    assert result["primary_name"] == "奥秘命令"
    # Both candidates were probed
    assert len(transport.calls) == 2
    assert "WRONG-48" in transport.calls[0]
    assert "RIGHT-48" in transport.calls[1]


def test_plst_slash_no_matching_total():
    # card_count=361 → no sets in SETS_DATA → null, zero network calls
    count_to_codes = _build_count_to_codes(SETS_DATA)
    client, transport = _make_client({})
    result = _resolve_plst_slash(client, "261/361", "Three Visits", count_to_codes)
    assert result == {}
    assert transport.calls == []


def test_plst_plain_format_unaffected():
    # collector_number "49" (no slash) → goes to _get_card, not slash resolver → 404 → null
    class _404Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

    client = httpx.Client(transport=_404Transport(), headers={"User-Agent": "test"})
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(SETS_DATA)
    count_to_codes = _build_count_to_codes(SETS_DATA)
    row = _row(set_name_en="The List", name_en="Opt", collector_number="49")
    result = _enrich_row(0, row, en_to_code, code_to_zh, client,
                         code_to_name=code_to_name, count_to_codes=count_to_codes)
    assert result["name_zh"] is None
    assert result["jihuanshe_price_cny"] is None


def test_plst_slash_all_candidates_wrong():
    # Both candidates return the wrong English name → null, not false-accept
    count_to_codes = {"249": ["WRONG", "RIGHT"]}
    routes = {
        "https://mtgch.com/api/v1/card/PLST/WRONG-48/?view=1": _card_named("Not The Card", "不是"),
        "https://mtgch.com/api/v1/card/PLST/RIGHT-48/?view=1": _card_named("Also Not The Card", "也不是"),
    }
    client, transport = _make_client(routes)
    result = _resolve_plst_slash(client, "048/249", "Target Card Name", count_to_codes)
    assert result == {}
    assert len(transport.calls) == 2  # exhausted all candidates


# ── name-search fallback ───────────────────────────────────────────────────────

def _search_url(name_en: str) -> str:
    """Build the exact URL that _search_card_by_name will request (strips parens first)."""
    bare = _strip_parenthetical(name_en)
    return str(httpx.URL(f"{BASE_URL}/result", params={
        "q": f'name:"{bare}"',
        "priority_chinese": "true",
        "unique": "oracle_id",
        "view": "0",
        "page_size": "50",
    }))


def _search_response(items: list[dict]) -> dict:
    return {"count": len(items), "page": 1, "page_size": 50, "total_pages": 1, "items": items}


def _search_item(name: str, official: str | None = None, translated: str | None = None,
                 set_code: str = "SLD", collector_number: str = "1") -> dict:
    return {
        "name": name,
        "atomic_official_name": official,
        "atomic_translated_name": translated,
        "set": set_code,
        "collector_number": collector_number,
        "prices": {"usd": "1.00"},
    }


def test_name_fallback_exact_match():
    # Search returns two items; only second exactly matches name_en and has a Chinese name
    items = [
        _search_item("Lightning Bolt Wand", official="闪电棒", set_code="XYZ"),  # wrong name
        _search_item("Lightning Bolt", official="闪电击", set_code="M10", collector_number="146"),
    ]
    routes = {_search_url("Lightning Bolt"): _search_response(items)}
    client, _ = _make_client(routes)
    result = _search_card_by_name(client, "Lightning Bolt")
    assert result is not None
    assert result["name_zh"] == "闪电击"
    assert result["is_official"] is True
    assert result["set"] == "M10"
    assert result["collector_number"] == "146"


def test_malformed_name_search_cache_is_refetched():
    url = _search_url("Lightning Bolt")
    path = enrich._url_cache_path(
        "cards", url, "_name_search", "Lightning_Bolt"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "_cache_kind": "card_response_v1",
        "fetched_at": time.time(),
        "data": {"name_zh": "缺少缓存字段"},
    }), encoding="utf-8")
    items = [_search_item(
        "Lightning Bolt", official="闪电击", set_code="M10", collector_number="146"
    )]
    client, transport = _make_client({url: _search_response(items)})

    result = _search_card_by_name(client, "Lightning Bolt")

    assert result is not None
    assert result["name_zh"] == "闪电击"
    assert transport.calls == [url]


def test_name_fallback_no_exact_match():
    # Search returns only fuzzy/partial matches; fallback returns None
    items = [
        _search_item("Lightning Bolt Wand", official="闪电棒"),
        _search_item("Chain Lightning", official="连锁闪电"),
    ]
    routes = {_search_url("Lightning Bolt"): _search_response(items)}
    client, _ = _make_client(routes)
    result = _search_card_by_name(client, "Lightning Bolt")
    assert result is None


def test_name_fallback_search_500():
    # Search endpoint returns 500; fallback returns None gracefully (no crash)
    class _500Transport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal server error"})

    client = httpx.Client(transport=_500Transport(), headers={"User-Agent": "test"})
    result = _search_card_by_name(client, "Demonic Tutor")
    assert result is None


def test_name_fallback_does_not_borrow_price():
    # Borrowed result must not contribute jihuanshe_price_cny even if search item has prices
    items = [_search_item("Demonic Tutor", official="邪魔导师", set_code="SLD", collector_number="1856")]
    search_routes = {_search_url("Demonic Tutor"): _search_response(items)}
    # Make the primary card lookup 404
    class _404Transport(httpx.BaseTransport):
        def __init__(self, fallback_routes):
            self._fb = fallback_routes
            self.calls = []
        def handle_request(self, request):
            url = str(request.url)
            self.calls.append(url)
            if url in self._fb:
                return httpx.Response(200, json=self._fb[url])
            return httpx.Response(404)

    client = httpx.Client(transport=_404Transport(search_routes), headers={"User-Agent": "test"})
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(SETS_DATA)
    row = _row(set_name_en="Modern Horizons 3", name_en="Demonic Tutor", collector_number="999")
    result = _enrich_row(0, row, en_to_code, code_to_zh, client,
                         code_to_name=code_to_name)
    assert result["name_zh"] == "邪魔导师"
    assert result["jihuanshe_price_cny"] is None  # must not borrow


def test_name_fallback_does_not_borrow_image():
    # Same setup — image_uri must stay null even if search result has image fields
    items = [_search_item("Demonic Tutor", official="邪魔导师", set_code="SLD", collector_number="1856")]
    search_routes = {_search_url("Demonic Tutor"): _search_response(items)}

    class _404Transport(httpx.BaseTransport):
        def __init__(self, fallback_routes):
            self._fb = fallback_routes
        def handle_request(self, request):
            url = str(request.url)
            if url in self._fb:
                return httpx.Response(200, json=self._fb[url])
            return httpx.Response(404)

    client = httpx.Client(transport=_404Transport(search_routes), headers={"User-Agent": "test"})
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(SETS_DATA)
    row = _row(set_name_en="Modern Horizons 3", name_en="Demonic Tutor", collector_number="999")
    result = _enrich_row(0, row, en_to_code, code_to_zh, client,
                         code_to_name=code_to_name)
    assert result["name_zh"] == "邪魔导师"
    assert result["sbwsz_image_uri"] is None  # must not borrow


# ── _strip_parenthetical ───────────────────────────────────────────────────────

def test_strip_parenthetical_single():
    assert _strip_parenthetical("Foo (DVD)") == "Foo"


def test_strip_parenthetical_double():
    assert _strip_parenthetical("Foo (A) (B)") == "Foo"


def test_strip_parenthetical_no_parens():
    assert _strip_parenthetical("Foo") == "Foo"


def test_strip_parenthetical_mid_string_preserved():
    # trailing group stripped; mid-string group preserved
    assert _strip_parenthetical("Name With (Middle) Parens And (Trailing)") == "Name With (Middle) Parens And"
    # no trailing group → unchanged
    assert _strip_parenthetical("Name With (Middle) Parens Only") == "Name With (Middle) Parens Only"


def test_name_fallback_strips_parenthetical_in_query():
    # search URL must use bare name; mock keyed on stripped URL would 404 if not stripped
    items = [_search_item("Demonic Tutor", official="邪魔导师")]
    routes = {_search_url("Demonic Tutor"): _search_response(items)}  # bare name key
    client, transport = _make_client(routes)
    result = _search_card_by_name(client, "Demonic Tutor (DVD)")
    assert result is not None
    assert result["name_zh"] == "邪魔导师"
    # Called URL must not contain the suffix
    assert len(transport.calls) == 1
    assert "DVD" not in transport.calls[0]


def test_name_fallback_strips_parenthetical_in_comparison():
    # item has bare name; row has parenthetical suffix — both sides stripped before compare
    items = [
        _search_item("Demonic Tutor (DVD)", official="假结果"),  # wrong: item with suffix, must be rejected
        _search_item("Demonic Tutor", official="邪魔导师"),       # correct: bare name matches
    ]
    routes = {_search_url("Demonic Tutor (DVD)"): _search_response(items)}
    client, _ = _make_client(routes)
    result = _search_card_by_name(client, "Demonic Tutor (DVD)")
    assert result is not None
    assert result["name_zh"] == "邪魔导师"


def test_plst_slash_strips_parenthetical():
    # PLST slash-format where name_en has trailing set code: "Cryptic Command (IMA)"
    # face_name from sbwsz is bare "Cryptic Command" — must match after strip
    count_to_codes = {"249": ["IMA"]}
    routes = {
        "https://mtgch.com/api/v1/card/PLST/IMA-48/?view=1": _card_named("Cryptic Command", "地下指命"),
    }
    client, _ = _make_client(routes)
    result = _resolve_plst_slash(client, "048/249", "Cryptic Command (IMA)", count_to_codes)
    assert result["primary_name"] == "地下指命"


# ── row ID assignment ──────────────────────────────────────────────────────────

def test_assign_row_ids_present():
    rows = [{"product_id": 1, "name_en": "A"}, {"product_id": 2, "name_en": "B"}]
    result = _assign_row_ids(rows)
    assert all("row_id" in r for r in result)


def test_assign_row_ids_unique():
    rows = [
        {"product_id": 1, "name_en": "A"},
        {"product_id": 1, "name_en": "A copy"},
        {"product_id": 2, "name_en": "B"},
        {"product_id": 2, "name_en": "B copy"},
    ]
    result = _assign_row_ids(rows)
    ids = [r["row_id"] for r in result]
    assert len(ids) == len(set(ids))


def test_assign_row_ids_copy_suffix():
    # Three copies of product 99, one copy of product 7
    rows = [
        {"product_id": 99}, {"product_id": 99}, {"product_id": 7}, {"product_id": 99},
    ]
    result = _assign_row_ids(rows)
    assert result[0]["row_id"] == "99_0"
    assert result[1]["row_id"] == "99_1"
    assert result[2]["row_id"] == "7_0"   # independent counter
    assert result[3]["row_id"] == "99_2"


def test_assign_row_ids_does_not_mutate_input():
    rows = [{"product_id": 5, "name_en": "X"}]
    _assign_row_ids(rows)
    assert "row_id" not in rows[0]  # original untouched


def test_assign_row_ids_are_stable_when_variants_are_reordered():
    foil = {
        "product_id": 99,
        "name_en": "Test Card",
        "set_name_en": "Test Set",
        "collector_number": "1",
        "printing": "Foil",
        "condition": "Near Mint",
    }
    normal = {**foil, "printing": "Normal"}

    forward = _assign_row_ids([foil, normal])
    reversed_result = _assign_row_ids([normal, foil])

    forward_ids = {row["printing"]: row["row_id"] for row in forward}
    reversed_ids = {row["printing"]: row["row_id"] for row in reversed_result}
    assert forward_ids == reversed_ids == {"Foil": "99_0", "Normal": "99_1"}
    assert [row["printing"] for row in reversed_result] == ["Normal", "Foil"]


# ── _normalize_for_compare ────────────────────────────────────────────────────

def test_normalize_basic():
    forms = _normalize_for_compare("Lightning Bolt")
    assert "lightning bolt" in forms


def test_normalize_strips_parenthetical():
    forms = _normalize_for_compare("Foo (DVD)")
    assert "foo" in forms
    assert "foo (dvd)" not in forms


def test_normalize_split_card():
    forms = _normalize_for_compare("Find // Finality")
    assert "find // finality" in forms
    assert "find" in forms           # first face extracted


def test_normalize_sld_dash():
    forms = _normalize_for_compare("Miku, Font of Pop - Giada, Font of Hope (Rainbow Foil)")
    # post-dash portion stripped of its trailing paren
    assert "giada, font of hope" in forms


def test_normalize_diacritic():
    forms = _normalize_for_compare("Arna Kennerüd, Skycaptain")
    assert "arna kennerud, skycaptain" in forms   # ü → u via NFKD + ASCII encode


def test_verify_match_split_card():
    # sbwsz returns face name "Find"; row has "Find // Finality" — must match
    assert _normalize_for_compare("Find // Finality") & _normalize_for_compare("Find")


def test_verify_match_sld_dash():
    # sbwsz returns "Giada, Font of Hope"; row has the full product-name prefix
    row_name = "Miku, Font of Pop - Giada, Font of Hope (Rainbow Foil)"
    assert _normalize_for_compare(row_name) & _normalize_for_compare("Giada, Font of Hope")


def test_verify_match_diacritic():
    # TCGPlayer: "Arna Kennerud"; sbwsz: "Arna Kennerüd" — must match
    assert _normalize_for_compare("Arna Kennerud, Skycaptain") & _normalize_for_compare("Arna Kennerüd, Skycaptain")


def test_verify_reject_wrong_card():
    # "Strike It Rich (Retro Frame)" vs sbwsz "Esper Sentinel" — must NOT match
    assert not (_normalize_for_compare("Strike It Rich (Retro Frame)") & _normalize_for_compare("Esper Sentinel"))


def test_verify_reject_completely_different():
    assert not (_normalize_for_compare("Lightning Bolt") & _normalize_for_compare("Counterspell"))
