from pathlib import Path

import httpx
import pytest

from mtg_xianyu import enrich
from mtg_xianyu.enrich import (
    _build_set_dicts,
    _enrich_row,
    _jihuanshe_price,
    _name_zh,
    _resolve_set_code,
)

# ── test data ──────────────────────────────────────────────────────────────────

SETS_DATA = [
    {"code": "MH3", "name": "Modern Horizons 3", "translated_name": "现代地平线3"},
    {"code": "SLD", "name": "Secret Lair Drop", "translated_name": "秘密巢穴"},
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
    en_to_code, _ = _build_set_dicts(SETS_DATA)
    assert _resolve_set_code("Modern Horizons 3", en_to_code, "test") == "MH3"


def test_fuzzy_set_match(capsys):
    en_to_code, _ = _build_set_dicts(SETS_DATA)
    code = _resolve_set_code("Modern Horizons III", en_to_code, "test")
    assert code == "MH3"
    assert "[fuzzy]" in capsys.readouterr().err


def test_unknown_set_raises():
    en_to_code, _ = _build_set_dicts(SETS_DATA)
    with pytest.raises(ValueError, match="no set_code"):
        _resolve_set_code("Completely Unknown Set XYZ 9999", en_to_code, "row ref")


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
    en_to_code, code_to_zh = _build_set_dicts(SETS_DATA)
    result = _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert result["name_zh"] is None
    assert result["jihuanshe_price_cny"] is None
    assert "[404]" in capsys.readouterr().err


# ── network / cache behaviour ──────────────────────────────────────────────────

def test_view_1_in_cached_url():
    routes = {"https://mtgch.com/api/v1/card/MH3/146/?view=1": _card()}
    client, transport = _make_client(routes)
    en_to_code, code_to_zh = _build_set_dicts(SETS_DATA)
    _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert transport.calls == ["https://mtgch.com/api/v1/card/MH3/146/?view=1"]


def test_cache_hit_skips_network():
    routes = {"https://mtgch.com/api/v1/card/MH3/146/?view=1": _card()}
    client, transport = _make_client(routes)
    en_to_code, code_to_zh = _build_set_dicts(SETS_DATA)

    _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert len(transport.calls) == 1

    _enrich_row(0, _row(), en_to_code, code_to_zh, client)
    assert len(transport.calls) == 1  # cache hit — no second request
