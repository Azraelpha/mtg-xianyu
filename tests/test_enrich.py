from pathlib import Path

import httpx
import pytest

from mtg_xianyu import enrich
from mtg_xianyu.enrich import (
    _build_count_to_codes,
    _build_set_dicts,
    _enrich_row,
    _jihuanshe_price,
    _name_zh,
    _resolve_plst_slash,
    _resolve_set_code,
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
