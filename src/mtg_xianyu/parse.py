"""Parse a TCGPlayer collection export (.numbers or .csv) into normalized rows.

Reads the export, normalises column names, and expands any row where
Add to Quantity > 1 into one logical row per physical card. Downstream
stages must never see multi-quantity rows. Writes rows.json.
"""

import argparse
import csv
import math
import sys
from pathlib import Path

from mtg_xianyu.storage import atomic_write_json

# Zip magic bytes — Numbers files are zip archives regardless of extension.
_ZIP_MAGIC = b"PK\x03\x04"
_REQUIRED_SOURCE_COLUMNS = (
    "Product Line",
    "Product ID",
    "Set Name",
    "Product Name",
    "Number",
    "Condition",
    "Printing",
    "Add to Quantity",
)


def _validate_source_columns(columns, source: object) -> None:
    """Fail before normalization when the export schema is incomplete."""
    available = set(columns)
    missing = [name for name in _REQUIRED_SOURCE_COLUMNS if name not in available]
    if missing:
        names = ", ".join(repr(name) for name in missing)
        raise ValueError(
            f"invalid TCGPlayer export {source}: missing required "
            f"column(s): {names}"
        )


def _is_numbers_file(path: Path) -> bool:
    with path.open("rb") as f:
        return f.read(4) == _ZIP_MAGIC


def _read_numbers(path: Path) -> list[dict]:
    import shutil
    import tempfile

    from numbers_parser import Document

    # numbers-parser validates the file extension; stage through a temp file
    # when the caller passes a misnamed export (e.g. collection.csv).
    if path.suffix.lower() != ".numbers":
        with tempfile.NamedTemporaryFile(suffix=".numbers", delete=False) as tmp:
            shutil.copy2(path, tmp.name)
            tmp_path = Path(tmp.name)
        try:
            return _read_numbers(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    doc = Document(str(path))
    table = doc.sheets[0].tables[0]
    rows = list(table.rows())
    if not rows:
        _validate_source_columns((), path)
    headers = [str(c.value) if c.value is not None else "" for c in rows[0]]
    _validate_source_columns(headers, path)
    return [
        {headers[i]: cell.value for i, cell in enumerate(row) if i < len(headers)}
        for row in rows[1:]
    ]


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        _validate_source_columns(reader.fieldnames or (), path)
        return list(reader)


def _str(val) -> str:
    return str(val).strip() if val is not None else ""


def _float(val) -> float | None:
    if val is None:
        return None
    try:
        result = float(str(val).replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _collector_number(val) -> str:
    """Convert a Numbers cell value to a canonical collector-number string.

    Numeric cells arrive as float (354.0 → "354"). Non-numeric cells arrive as
    str and must pass through unchanged: "15a", "L1", "IFIYW-7", "★15", etc.
    We also handle the degenerate case where a text-typed cell contains a plain
    number string ("354" or "354.0") by attempting int(float(...)) first and
    only falling back to the raw string on ValueError.
    """
    if val is None:
        return ""
    if isinstance(val, (int, float)):
        number = float(val)
        if not math.isfinite(number):
            return ""
        return str(int(number)) if number.is_integer() else str(val).strip()
    s = str(val).strip()
    try:
        number = float(s)
        if math.isfinite(number) and number.is_integer():
            return str(int(number))
        return s
    except ValueError:
        return s


_REQUIRED_FIELDS = (
    "product_id",
    "name_en",
    "collector_number",
    "set_name_en",
    "condition",
    "printing",
)


def _positive_int(val, field: str, ref: str, *, default: int | None = None) -> int:
    text = _str(val)
    if not text and default is not None:
        return default
    try:
        number = float(text)
    except ValueError as exc:
        raise ValueError(f"{field!r} must be a positive integer in {ref}: {val!r}") from exc
    if not math.isfinite(number) or not number.is_integer() or number < 1:
        raise ValueError(f"{field!r} must be a positive integer in {ref}: {val!r}")
    return int(number)


def _normalize(raw_rows: list[dict]) -> list[dict]:
    if raw_rows:
        _validate_source_columns(raw_rows[0].keys(), "input rows")

    result = []
    for idx, raw in enumerate(raw_rows):
        if "Magic" not in _str(raw.get("Product Line")):
            continue

        ref = f"source row {idx} ({_str(raw.get('Product Name'))!r})"
        qty = _positive_int(
            raw.get("Add to Quantity"), "Add to Quantity", ref, default=1
        )
        product_id = _positive_int(raw.get("Product ID"), "Product ID", ref)

        row = {
            "product_id": product_id,
            "set_name_en": _str(raw.get("Set Name")),
            "name_en": _str(raw.get("Product Name")),
            "collector_number": _collector_number(raw.get("Number")),
            "rarity": _str(raw.get("Rarity")),
            "condition": _str(raw.get("Condition")),
            "printing": _str(raw.get("Printing")),
            "usd_market": _float(raw.get("TCG Market Price")),
            "tcg_photo_url": _str(raw.get("Photo URL")),
        }

        for field in _REQUIRED_FIELDS:
            if not row[field]:
                raise ValueError(f"{field!r} is empty in {ref}")
        if row["printing"] not in {"Normal", "Foil"}:
            raise ValueError(
                f"'printing' must be 'Normal' or 'Foil' in {ref}: "
                f"{row['printing']!r}"
            )

        for _ in range(qty):
            result.append(dict(row))

    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse a TCGPlayer export into rows.json."
    )
    parser.add_argument("input", help="Path to .numbers or .csv export file")
    parser.add_argument(
        "-o",
        "--output",
        default="data/rows.json",
        help="Output path (default: data/rows.json)",
    )
    args = parser.parse_args()

    path = Path(args.input)
    if not path.exists():
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    raw = _read_numbers(path) if _is_numbers_file(path) else _read_csv(path)
    source_count = len(raw)

    rows = _normalize(raw)

    out = Path(args.output)
    atomic_write_json(out, rows)

    expanded = len(rows) - source_count
    suffix = f" ({expanded:+d} from quantity expansion)" if expanded else ""
    print(f"wrote {len(rows)} rows{suffix} → {out}")


if __name__ == "__main__":
    main()
