from pathlib import Path

import pytest

from mtg_xianyu.parse import _collector_number, _normalize, _read_csv


def _row(**overrides):
    """Minimal valid TCGPlayer export row; override any column by keyword."""
    base = {
        "Product Line": "Magic: The Gathering",
        "Set Name": "Test Set",
        "Product Name": "Test Card",
        "Number": 1.0,
        "Rarity": "Common",
        "Condition": "Near Mint",
        "Printing": "Normal",
        "TCG Market Price": 1.0,
        "Photo URL": "https://example.com/1.jpg",
        "Product ID": 12345.0,
        "Add to Quantity": 1.0,
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# Multi-quantity expansion
# ---------------------------------------------------------------------------

def test_multi_quantity_expansion():
    rows = _normalize([
        _row(**{"Product Name": "Card A", "Add to Quantity": 3.0}),
        _row(**{"Product Name": "Card B", "Add to Quantity": 1.0}),
    ])
    assert len(rows) == 4
    assert sum(1 for r in rows if r["name_en"] == "Card A") == 3
    assert sum(1 for r in rows if r["name_en"] == "Card B") == 1


# ---------------------------------------------------------------------------
# _collector_number coercion
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("val, expected", [
    (354,       "354"),       # int
    (354.0,     "354"),       # float (numbers-parser numeric cell)
    ("354",     "354"),       # plain string
    ("354.0",   "354"),       # string with decimal from text-typed cell
    ("15a",     "15a"),       # split card
    ("IFIYW-7", "IFIYW-7"),   # Secret Lair drop
    ("★15",     "★15"),       # star promo
])
def test_collector_number_variants(val, expected):
    assert _collector_number(val) == expected


# ---------------------------------------------------------------------------
# Foil vs Normal preserved as distinct rows
# ---------------------------------------------------------------------------

def test_foil_and_normal_preserved():
    rows = _normalize([
        _row(Printing="Normal"),
        _row(Printing="Foil"),
    ])
    assert len(rows) == 2
    assert {r["printing"] for r in rows} == {"Normal", "Foil"}


# ---------------------------------------------------------------------------
# Fail-fast on missing required fields
# ---------------------------------------------------------------------------

def test_missing_collector_number_raises():
    with pytest.raises(ValueError, match="collector_number"):
        _normalize([_row(**{"Number": None})])


def test_missing_set_name_raises():
    with pytest.raises(ValueError, match="set_name_en"):
        _normalize([_row(**{"Set Name": ""})])


def test_missing_condition_raises():
    with pytest.raises(ValueError, match="condition"):
        _normalize([_row(**{"Condition": None})])


@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_printing_raises_with_row_context(value):
    with pytest.raises(ValueError, match=r"printing.*source row 0"):
        _normalize([_row(Printing=value)])


@pytest.mark.parametrize("column", ["Product Line", "Add to Quantity"])
def test_missing_required_source_column_raises_before_normalizing(column):
    row = _row()
    del row[column]

    with pytest.raises(ValueError, match=rf"missing required column.*{column}"):
        _normalize([row])


def test_header_only_csv_still_validates_required_columns(tmp_path: Path):
    path = tmp_path / "collection.csv"
    path.write_text("Product Line,Product ID\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"missing required column.*Set Name"):
        _read_csv(path)


@pytest.mark.parametrize("qty", [0, -1, 1.5, "abc", float("nan")])
def test_invalid_quantity_raises_with_row_context(qty):
    with pytest.raises(ValueError, match=r"Add to Quantity.*source row 0"):
        _normalize([_row(**{"Add to Quantity": qty})])


def test_missing_product_id_raises():
    with pytest.raises(ValueError, match="Product ID"):
        _normalize([_row(**{"Product ID": None})])


def test_missing_card_name_raises():
    with pytest.raises(ValueError, match="name_en"):
        _normalize([_row(**{"Product Name": ""})])


def test_unknown_printing_raises():
    with pytest.raises(ValueError, match="printing"):
        _normalize([_row(Printing="Etched")])
