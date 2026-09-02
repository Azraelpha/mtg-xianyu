from types import SimpleNamespace

import httpx
import pytest

from mtg_xianyu import enrich, ui


class _Transport(httpx.BaseTransport):
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(200, json=self.payload)


def test_legacy_card_cache_is_refreshed_once(tmp_path, monkeypatch):
    monkeypatch.setattr(
        enrich,
        "_cache_path",
        lambda kind, *parts: tmp_path / kind / ("_".join(parts) + ".json"),
    )
    path = enrich._cache_path("cards", "MH3", "1")
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
