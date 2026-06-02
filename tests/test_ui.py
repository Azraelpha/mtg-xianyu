from mtg_xianyu.ui import FX_RATE, effective_cny, sort_rows


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


def test_sort_rows_jhs_beats_usd_fallback():
    # A row with JHS ¥30 should rank above a row with USD $10 × 7.25 = ¥72.5
    # when the JHS row's price is higher
    rows = [
        {"jihuanshe_price_cny": None, "usd_market": 10.0},   # effective = 72.5
        {"jihuanshe_price_cny": 100.0, "usd_market": 1.0},   # effective = 100.0
    ]
    result = sort_rows(rows, "Effective CNY")
    assert result[0]["jihuanshe_price_cny"] == 100.0
