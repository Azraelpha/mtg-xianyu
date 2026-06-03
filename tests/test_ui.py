from mtg_xianyu.ui import FX_RATE, _approval_hints, effective_cny, sort_rows


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


def test_sort_rows_jhs_beats_usd_fallback():
    # A row with JHS ¥30 should rank above a row with USD $10 × 7.25 = ¥72.5
    # when the JHS row's price is higher
    rows = [
        {"jihuanshe_price_cny": None, "usd_market": 10.0},   # effective = 72.5
        {"jihuanshe_price_cny": 100.0, "usd_market": 1.0},   # effective = 100.0
    ]
    result = sort_rows(rows, "Effective CNY")
    assert result[0]["jihuanshe_price_cny"] == 100.0
