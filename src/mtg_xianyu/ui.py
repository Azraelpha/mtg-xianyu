"""Streamlit review UI for MTG → Xianyu listings.

Loads data/enriched.json, sorts by effective CNY price descending, and
displays one card at a time for photo binding and approval.

Run with:
    uv run streamlit run src/mtg_xianyu/ui.py

Must be run from the project root so that relative data/ paths resolve.
"""

import io
import json
import math
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener
import streamlit as st

from mtg_xianyu.describe import build_description, build_finish_zh

register_heif_opener()  # enable HEIC support for PIL.Image.open()

FX_RATE = 7.25          # CNY per USD — placeholder, tune after first sales
LISTINGS_DIR = Path("data/listings")
STATE_PATH = LISTINGS_DIR / "state.json"
PHOTO_DIR = Path("data/mtg_photos")
THUMBNAILS_PER_PAGE = 12
THUMBNAIL_COLS = 4

_PHOTO_EXTS = (
    "*.HEIC", "*.heic",
    "*.jpg", "*.jpeg", "*.JPG", "*.JPEG",
    "*.png", "*.PNG",
)


# ── pure helpers (testable without Streamlit) ─────────────────────────────────

def effective_cny(row: dict) -> float:
    """Return the best available CNY price for sorting and display.

    Preference order: JHS price → USD × FX_RATE → 0.0 (last resort).
    """
    if row.get("jihuanshe_price_cny") is not None:
        return row["jihuanshe_price_cny"]
    if row.get("usd_market") is not None:
        return row["usd_market"] * FX_RATE
    return 0.0


def sort_rows(rows: list[dict], sort_by: str) -> list[dict]:
    """Return a new sorted list; does not mutate input."""
    if sort_by == "USD market":
        return sorted(rows, key=lambda r: r.get("usd_market") or 0.0, reverse=True)
    return sorted(rows, key=effective_cny, reverse=True)


_VIEW_FILTERS: dict[str, set[str]] = {
    "Default":        {"ready_to_review", "approved"},
    "All rows":       {"waiting_photo", "ready_to_review", "approved", "skipped"},
    "Remaining only": {"waiting_photo", "ready_to_review"},
    "Skipped only":   {"skipped"},
}


def _filter_rows(rows: list[dict], state: dict, view: str) -> list[dict]:
    """Return rows whose current state is in the allowed set for view."""
    allowed = _VIEW_FILTERS[view]
    return [r for r in rows if _row_state(state, r.get("row_id", "")) in allowed]


# ── state I/O ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Read state.json. Returns empty structure if file doesn't exist."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"fx_rate": FX_RATE, "rows": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False, allow_nan=False)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=STATE_PATH.parent,
            prefix=f".{STATE_PATH.name}.",
            delete=False,
        ) as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, STATE_PATH)
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def _ensure_row(state: dict, row_id: str) -> None:
    """Add a default entry for row_id if it isn't already in state."""
    if row_id not in state.setdefault("rows", {}):
        state["rows"][row_id] = {
            "state": "waiting_photo",
            "photo_path": None,
            "name_zh_override": None,
            "price_cny": None,
            "price_source": None,
            "price_error": None,
            "approved_at": None,
        }


def _init_rows(state: dict, all_rows: list[dict]) -> bool:
    """Add missing row entries with initial waiting_photo state.

    Returns True if any new entries were added (caller should save).
    """
    changed = False
    for row in all_rows:
        row_id = row.get("row_id")
        if row_id and row_id not in state.get("rows", {}):
            _ensure_row(state, row_id)
            changed = True
    return changed


def _row_state(state: dict, row_id: str) -> str:
    return state.get("rows", {}).get(row_id, {}).get("state", "waiting_photo")


def _set_row_state(state: dict, row_id: str, **fields) -> None:
    """Partial-update a row's state entry and persist to disk."""
    _ensure_row(state, row_id)
    state["rows"][row_id].update(fields)
    _save_state(state)


# ── photo pool ────────────────────────────────────────────────────────────────

def _scan_photos() -> list[Path]:
    """Return all photo files in PHOTO_DIR sorted by filename."""
    if not PHOTO_DIR.exists():
        return []
    photos: set[Path] = set()
    for ext in _PHOTO_EXTS:
        photos.update(PHOTO_DIR.rglob(ext))
    return sorted(photos, key=lambda p: p.name)


def _build_unbound_pool(state: dict) -> list[Path]:
    """Return photos not yet bound to any row."""
    bound = {
        Path(r["photo_path"])
        for r in state.get("rows", {}).values()
        if r.get("photo_path")
    }
    return [p for p in _scan_photos() if p not in bound]


def _refresh_pool(state: dict) -> None:
    """Re-scan photos and rebuild the unbound pool."""
    st.session_state.unbound_pool = _build_unbound_pool(state)
    st.session_state.thumbnail_page = 0


# ── photo bind / unbind ───────────────────────────────────────────────────────

def _bind_photo(row_id: str, photo_path: Path, state: dict) -> None:
    """Bind photo_path to row_id; return any previously-bound photo to the pool."""
    row_entry = state.get("rows", {}).get(row_id, {})
    old_photo_str = row_entry.get("photo_path")

    # Persist new binding
    _ensure_row(state, row_id)
    state["rows"][row_id]["photo_path"] = str(photo_path)
    state["rows"][row_id]["state"] = "ready_to_review"
    _save_state(state)

    # Update pool: remove new, add back old (if different from new)
    pool = [p for p in st.session_state.unbound_pool if p != photo_path]
    if old_photo_str:
        old_path = Path(old_photo_str)
        if old_path != photo_path and old_path not in pool:
            pool.append(old_path)
            pool = sorted(pool, key=lambda p: p.name)
    st.session_state.unbound_pool = pool

    # Invalidate display_rows so the filter re-evaluates this row's new state
    st.session_state.pop("display_rows", None)
    st.rerun()


def _unbind_photo(row_id: str, state: dict) -> None:
    """Unbind current photo; return it to pool. Preserves name/price edits."""
    row_entry = state.get("rows", {}).get(row_id, {})
    old_photo_str = row_entry.get("photo_path")

    _ensure_row(state, row_id)
    state["rows"][row_id]["photo_path"] = None
    state["rows"][row_id]["state"] = "waiting_photo"
    _save_state(state)

    if old_photo_str:
        old_path = Path(old_photo_str)
        pool = st.session_state.unbound_pool
        if old_path not in pool:
            pool = sorted(pool + [old_path], key=lambda p: p.name)
            st.session_state.unbound_pool = pool

    st.session_state.pop("display_rows", None)
    st.rerun()


# ── image loading (cached to avoid reloading on every rerun) ──────────────────

@st.cache_data
def _load_thumbnail(path_str: str) -> bytes:
    img = Image.open(path_str)
    img.thumbnail((150, 300))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return buf.getvalue()


@st.cache_data
def _load_display_image(path_str: str) -> bytes:
    img = Image.open(path_str)
    img.thumbnail((800, 1200))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


@st.cache_data
def _photo_can_open(path_str: str, mtime_ns: int) -> bool:
    """Return whether Pillow can decode the current version of a photo."""
    del mtime_ns  # included in the cache key so replacing a file invalidates it
    try:
        with Image.open(path_str) as img:
            img.verify()
        return True
    except (OSError, ValueError):
        return False


# ── on_change callbacks ───────────────────────────────────────────────────────

def _on_name_change(row_id: str) -> None:
    val = st.session_state.get(f"name_zh_{row_id}", "").strip()
    state = st.session_state.state
    _ensure_row(state, row_id)
    state["rows"][row_id]["name_zh_override"] = val if val else None
    _save_state(state)
    st.rerun()


def _parse_manual_price(value: str) -> float:
    """Parse a finite, non-negative CNY amount or raise a useful error."""
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Price must be a number") from exc
    if not math.isfinite(price):
        raise ValueError("Price must be finite")
    if price < 0:
        raise ValueError("Price cannot be negative")
    return price


def _on_price_override_change(row_id: str) -> None:
    val = st.session_state.get(f"price_override_{row_id}", "").strip()
    state = st.session_state.state
    _ensure_row(state, row_id)
    if val:
        try:
            state["rows"][row_id]["price_cny"] = _parse_manual_price(val)
            state["rows"][row_id]["price_source"] = "manual"
            state["rows"][row_id]["price_error"] = None
        except ValueError as exc:
            state["rows"][row_id]["price_cny"] = None
            state["rows"][row_id]["price_source"] = None
            state["rows"][row_id]["price_error"] = str(exc)
    else:
        if state["rows"][row_id].get("price_source") == "manual":
            state["rows"][row_id]["price_cny"] = None
            state["rows"][row_id]["price_source"] = None
        state["rows"][row_id]["price_error"] = None
    _save_state(state)
    st.rerun()


# ── data loading ──────────────────────────────────────────────────────────────

@st.cache_data
def _load_enriched() -> list[dict]:
    path = Path("data/enriched.json")
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


# ── approval readiness ───────────────────────────────────────────────────────

def _approval_hints(row: dict, row_entry: dict) -> list[tuple[str, str]]:
    """Return (message, level) pairs summarising what's left before approval.

    level is 'warning', 'info', or 'success'.  Only meaningful for
    ready_to_review rows; callers gate on that state.
    """
    hints: list[tuple[str, str]] = []

    price_cny = row_entry.get("price_cny")
    jhs = row.get("jihuanshe_price_cny")
    usd = row.get("usd_market")

    if row_entry.get("price_error"):
        hints.append((f"⚠ Invalid manual price: {row_entry['price_error']}", "warning"))
    elif price_cny is None:
        if jhs is not None:
            hints.append((
                f"✓ Price will default to JHS ¥{jhs:.2f} (or click Use this to confirm)",
                "info",
            ))
        elif usd is not None:
            hints.append((
                f"✓ Price will default to USD × FX ¥{usd * FX_RATE:.2f}"
                " (or click Use this to confirm)",
                "info",
            ))
        else:
            hints.append(("⚠ Set a price before approving", "warning"))

    name_zh_override = row_entry.get("name_zh_override") or ""
    if not name_zh_override and not row.get("name_zh"):
        hints.append(("⚠ Add a Chinese name before approving", "warning"))

    if "photo_path" in row_entry:
        photo_path = row_entry.get("photo_path")
        path = Path(photo_path) if photo_path else None
        if path is None or not path.is_file():
            hints.append(("⚠ Bind an existing photo before approving", "warning"))
        elif not _photo_can_open(str(path), path.stat().st_mtime_ns):
            hints.append(("⚠ Bound photo is unreadable; bind another photo", "warning"))

    if "set_code" in row and not row.get("set_code"):
        hints.append((
            "⚠ Set identity is unresolved; fix enrichment before approving",
            "warning",
        ))

    if not hints:
        hints.append(("✓ Ready to approve", "success"))

    return hints


# ── approve / skip actions ───────────────────────────────────────────────────

def _resolve_final_price_and_source(
    row: dict, state_row: dict, fx_rate: float
) -> tuple[float, str]:
    """Return (price_cny, price_source) for the approval listing.

    Precedence: explicit state (manual/jhs/usd) → JHS default → USD default.
    Raises ValueError only if no price is available at all (should be
    unreachable when Approve is enabled, since the hint blocks it).
    """
    if state_row.get("price_error"):
        raise ValueError(
            f"Invalid manual price for row {row.get('row_id')}: "
            f"{state_row['price_error']}"
        )
    if state_row.get("price_source") is not None:
        return (
            _parse_manual_price(str(state_row.get("price_cny"))),
            state_row["price_source"],
        )
    jhs = row.get("jihuanshe_price_cny")
    if jhs is not None:
        return _parse_manual_price(str(jhs)), "jhs"
    usd = row.get("usd_market")
    if usd is not None:
        return _parse_manual_price(str(round(usd * fx_rate, 2))), "usd_converted"
    raise ValueError(f"No price available for row {row.get('row_id')}")


def _build_listing(row: dict, state_row: dict, fx_rate: float, ts: str) -> dict:
    """Assemble the listing dict written to data/listings/{row_id}.json."""
    row_id = row["row_id"]
    final_price, final_source = _resolve_final_price_and_source(row, state_row, fx_rate)
    return {
        "row_id": row_id,
        "product_id": row.get("product_id"),
        "name_en": row.get("name_en"),
        "name_zh": state_row.get("name_zh_override") or row.get("name_zh"),
        "set_code": row.get("set_code"),
        "set_name_en": row.get("set_name_en"),
        "set_name_zh": row.get("set_name_zh"),
        "collector_number": row.get("collector_number"),
        "condition": row.get("condition"),
        "printing": row.get("printing"),
        "rarity": row.get("rarity"),
        "jihuanshe_price_cny": row.get("jihuanshe_price_cny"),
        "usd_market": row.get("usd_market"),
        "price_cny": final_price,
        "price_source": final_source,
        "fx_rate_at_approval": fx_rate,
        "photo_jpg": str(LISTINGS_DIR / f"{row_id}.jpg"),
        "photo_heic_source": state_row.get("photo_path"),
        "approved_at": ts,
    }


def _next_unfinished_idx(
    display_rows: list[dict], state: dict, current_idx: int
) -> int | None:
    """Return the next ready row's index in the currently applied view."""
    next_row_id = None
    for i in range(current_idx + 1, len(display_rows)):
        if _row_state(state, display_rows[i].get("row_id", "")) == "ready_to_review":
            next_row_id = display_rows[i].get("row_id")
            break
    if next_row_id is None:
        return None

    try:
        view = st.session_state.get("_applied_view", "All rows")
    except Exception:
        view = "All rows"
    if view not in _VIEW_FILTERS:
        view = "All rows"
    rebuilt_rows = _filter_rows(display_rows, state, view)
    for i, row in enumerate(rebuilt_rows):
        if row.get("row_id") == next_row_id:
            return i
    return None


_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]+\)\s*$")


def _safe_name_en(name_en: str) -> str:
    """Strip parentheticals and sanitize name_en for use in a filename."""
    s = name_en or ""
    while _TRAILING_PAREN_RE.search(s):
        s = _TRAILING_PAREN_RE.sub("", s).strip()
    s = s.replace("/", "-").replace(":", "-")
    return " ".join(s.split()).strip()


def _jpg_path_for(listing: dict, listings_dir: Path | None = None) -> Path:
    """Build the human-readable JPEG path for a listing, with collision fallback."""
    listings_dir = LISTINGS_DIR if listings_dir is None else listings_dir
    row_id = listing.get("row_id", "unknown")
    name_safe = _safe_name_en(listing.get("name_en", ""))

    if not name_safe:
        return listings_dir / f"{row_id}.jpg"

    finish_zh = build_finish_zh(listing["name_en"], listing["printing"])
    cn_safe = listing.get("collector_number", "").replace("/", "-")
    stem = (
        f"{listing['set_code']}-{cn_safe}"
        f" - {name_safe}"
        f" - {finish_zh}"
    )
    candidate = listings_dir / f"{stem}.jpg"
    if candidate.exists():
        copy_num = str(row_id).rsplit("_", 1)[-1]
        return listings_dir / f"{stem} (copy {copy_num}).jpg"
    return candidate


def _prepare_text_file(target: Path, payload: str) -> Path:
    """Write and fsync a temporary sibling for a future atomic replace."""
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=target.parent,
        prefix=f".{target.name}.",
        delete=False,
    ) as tmp:
        tmp.write(payload)
        tmp.flush()
        os.fsync(tmp.fileno())
        return Path(tmp.name)


def _write_listing_artifacts(
    listing: dict,
    photo_path: str,
    jpg_path: Path,
    json_path: Path,
    txt_path: Path,
) -> list[Path]:
    """Prepare every artifact first, then atomically install the complete set."""
    final_paths = [jpg_path, json_path, txt_path]
    existing = [path for path in final_paths if path.exists()]
    if existing:
        names = ", ".join(path.name for path in existing)
        raise FileExistsError(f"Refusing to overwrite existing listing artifacts: {names}")

    temp_paths: list[Path] = []
    installed: list[Path] = []
    try:
        with tempfile.NamedTemporaryFile(
            dir=jpg_path.parent,
            prefix=f".{jpg_path.name}.",
            suffix=".jpg",
            delete=False,
        ) as tmp:
            jpg_tmp = Path(tmp.name)
        temp_paths.append(jpg_tmp)
        with Image.open(photo_path) as source:
            converted = source.convert("RGB")
            try:
                converted.save(jpg_tmp, format="JPEG", quality=90, optimize=True)
            finally:
                converted.close()
        with jpg_tmp.open("rb") as prepared_jpg:
            os.fsync(prepared_jpg.fileno())

        json_tmp = _prepare_text_file(
            json_path,
            json.dumps(listing, indent=2, ensure_ascii=False, allow_nan=False),
        )
        temp_paths.append(json_tmp)
        txt_tmp = _prepare_text_file(txt_path, build_description(listing))
        temp_paths.append(txt_tmp)

        for temp_path, final_path in zip(temp_paths, final_paths, strict=True):
            os.replace(temp_path, final_path)
            installed.append(final_path)
        return installed
    except Exception:
        for path in reversed(installed):
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in temp_paths:
            path.unlink(missing_ok=True)


def _do_approve(row: dict, row_id: str, state: dict) -> None:
    """Atomically create listing artifacts, then persist the approved state."""
    row_entry = state["rows"][row_id]
    original_row_entry = dict(row_entry)
    ts = datetime.now().isoformat(timespec="seconds")
    listing = _build_listing(row, row_entry, FX_RATE, ts)

    LISTINGS_DIR.mkdir(parents=True, exist_ok=True)

    jpg_path = _jpg_path_for(listing)
    listing["photo_jpg"] = str(jpg_path)   # overwrite default set by _build_listing
    json_path = LISTINGS_DIR / f"{row_id}.json"
    txt_path = LISTINGS_DIR / f"{row_id}.txt"
    installed = _write_listing_artifacts(
        listing,
        row_entry["photo_path"],
        jpg_path,
        json_path,
        txt_path,
    )

    state["rows"][row_id].update({
        "state": "approved",
        "approved_at": ts,
        "price_cny": listing["price_cny"],
        "price_source": listing["price_source"],
        "price_error": None,
    })
    try:
        _save_state(state)
    except Exception:
        state["rows"][row_id] = original_row_entry
        for path in reversed(installed):
            path.unlink(missing_ok=True)
        raise


# ── UI ────────────────────────────────────────────────────────────────────────

def _render_app() -> None:
    st.set_page_config(
        page_title="MTG → Xianyu Review",
        page_icon="🃏",
        layout="wide",
    )

    # ── sidebar ───────────────────────────────────────────────────────────────
    with st.sidebar:
        st.header("Display options")
        st.metric("FX Rate (CNY/USD)", FX_RATE)
        sort_by = st.radio(
            "Sort by",
            ["Effective CNY", "USD market"],
            key="sort_radio",
        )
        st.caption("Changes review order, not displayed prices.")
        view = st.radio(
            "View",
            ["Default", "All rows", "Remaining only", "Skipped only"],
            key="view_radio",
        )

    # ── load enriched data ────────────────────────────────────────────────────
    all_rows = _load_enriched()
    if not all_rows:
        st.error("data/enriched.json not found. Run mtg-enrich first.")
        return

    # ── init state (once per session; re-init on page refresh) ───────────────
    if "state" not in st.session_state:
        state = _load_state()
        if _init_rows(state, all_rows):
            _save_state(state)
        st.session_state.state = state

    state = st.session_state.state

    # ── sidebar progress indicator ────────────────────────────────────────────
    with st.sidebar:
        st.divider()
        _all_states = list(state.get("rows", {}).values())
        n_approved  = sum(1 for r in _all_states if r.get("state") == "approved")
        n_skipped   = sum(1 for r in _all_states if r.get("state") == "skipped")
        n_remaining = len(all_rows) - n_approved - n_skipped
        st.markdown(
            f"Progress: **{n_approved}** of {len(all_rows)} approved · "
            f"**{n_skipped}** skipped · **{n_remaining}** remaining"
        )
        if st.button("📁 Open listings folder", use_container_width=True):
            subprocess.run(["open", str(LISTINGS_DIR)])

    # ── init photo pool (once per session) ───────────────────────────────────
    if "unbound_pool" not in st.session_state:
        st.session_state.unbound_pool = _build_unbound_pool(state)
        st.session_state.thumbnail_page = 0

    # ── rebuild display list ──────────────────────────────────────────────────
    # Rebuild when sort/filter changes (→ reset index) OR when a bind/unbind
    # invalidated the cache (→ preserve index).
    sort_changed = st.session_state.get("_applied_sort") != sort_by
    filter_changed = st.session_state.get("_applied_view") != view
    needs_rebuild = "display_rows" not in st.session_state or sort_changed or filter_changed

    if needs_rebuild:
        sorted_all = sort_rows(all_rows, sort_by)
        display_rows = _filter_rows(sorted_all, state, view)
        st.session_state.display_rows = display_rows
        st.session_state._applied_sort = sort_by
        st.session_state._applied_view = view
        if sort_changed or filter_changed:
            st.session_state.current_idx = 0

    if "current_idx" not in st.session_state:
        st.session_state.current_idx = 0

    display_rows = st.session_state.display_rows
    total = len(display_rows)

    # ── empty state ───────────────────────────────────────────────────────────
    if total == 0:
        st.markdown("---")
        if view == "Remaining only":
            st.success("No remaining work — all approved or skipped.")
        elif view == "Skipped only":
            st.info("No skipped rows.")
        else:
            st.info("No rows match the current view.")
        return

    # ── guard index ───────────────────────────────────────────────────────────
    idx = min(st.session_state.current_idx, total - 1)
    st.session_state.current_idx = idx
    row = display_rows[idx]
    row_id = row.get("row_id", str(idx))

    # Row-level state
    row_entry = state.get("rows", {}).get(row_id, {})
    price_source = row_entry.get("price_source")
    bound_photo = row_entry.get("photo_path")

    # Pre-populate widget session-state from disk (survives page refresh)
    name_key = f"name_zh_{row_id}"
    if name_key not in st.session_state:
        override = row_entry.get("name_zh_override")
        st.session_state[name_key] = override if override is not None else (row.get("name_zh") or "")

    price_key = f"price_override_{row_id}"
    if price_key not in st.session_state:
        val = row_entry.get("price_cny") if price_source == "manual" else ""
        st.session_state[price_key] = str(val) if val else ""

    # ── top bar ───────────────────────────────────────────────────────────────
    name_en   = row.get("name_en", "?")
    name_zh   = st.session_state.get(name_key) or row.get("name_zh") or "?"
    set_en    = row.get("set_name_en", "?")
    set_zh    = row.get("set_name_zh") or ""
    cn        = row.get("collector_number", "?")
    condition = row.get("condition", "?")
    printing  = row.get("printing", "Normal")
    rarity    = row.get("rarity", "?")
    foil_badge = "✨ Foil" if printing == "Foil" else "Normal"
    set_label  = f"{set_en} / {set_zh}" if set_zh else set_en

    st.markdown(f"### {name_en} — {name_zh}")
    st.caption(f"{set_label} · #{cn} · {condition} · {foil_badge} · {rarity}")
    st.divider()

    # ── per-row state (computed once, used in both columns) ──────────────────
    current_row_state = row_entry.get("state", "waiting_photo")
    hints_for_approval = (
        _approval_hints(row, row_entry)
        if current_row_state in ("ready_to_review", "skipped")
        else []
    )
    blocking = any(level == "warning" for _, level in hints_for_approval)
    if current_row_state == "skipped":
        blocking = blocking or not bound_photo  # no photo → can't convert to JPEG

    # ── middle row ────────────────────────────────────────────────────────────
    left, right = st.columns([3, 2])

    with left:
        # Row counter + state badge
        if current_row_state == "approved":
            badge = ("<span style='background:#d4edda;color:#155724;"
                     "padding:2px 8px;border-radius:4px;"
                     "font-size:0.8em;font-weight:bold'>✓ APPROVED</span>")
        elif current_row_state == "skipped":
            badge = ("<span style='background:#fff3cd;color:#856404;"
                     "padding:2px 8px;border-radius:4px;"
                     "font-size:0.8em;font-weight:bold'>⊘ SKIPPED</span>")
        elif current_row_state == "ready_to_review":
            badge = ("<span style='background:#d1ecf1;color:#0c5460;"
                     "padding:2px 8px;border-radius:4px;"
                     "font-size:0.8em'>● READY</span>")
        else:
            badge = ("<span style='background:#e2e3e5;color:#6c757d;"
                     "padding:2px 8px;border-radius:4px;"
                     "font-size:0.8em'>● WAITING PHOTO</span>")
        st.markdown(
            f"<small>Row {idx + 1} of {total}</small> {badge}",
            unsafe_allow_html=True,
        )

        nav_prev, nav_next = st.columns(2)
        with nav_prev:
            if st.button("← Prev", disabled=(idx == 0), use_container_width=True):
                st.session_state.current_idx = idx - 1
                st.session_state.pop("_all_caught_up", None)
                st.rerun()
        with nav_next:
            if st.button("Next →", disabled=(idx == total - 1), use_container_width=True):
                st.session_state.current_idx = idx + 1
                st.session_state.pop("_all_caught_up", None)
                st.rerun()

        st.markdown("<br>", unsafe_allow_html=True)

        # ── bound photo or placeholder ────────────────────────────────────────
        if bound_photo and Path(bound_photo).exists():
            try:
                st.image(_load_display_image(bound_photo), width=400)
            except Exception as exc:
                st.warning(f"Cannot open {Path(bound_photo).name}: {exc}")
            if st.button("✕ Unbind", key=f"unbind_{row_id}"):
                _unbind_photo(row_id, state)
        elif bound_photo:
            st.warning(f"Photo file missing: {Path(bound_photo).name}")
            if st.button("✕ Unbind (file missing)", key=f"unbind_{row_id}"):
                _unbind_photo(row_id, state)
        else:
            st.markdown(
                "<div style='height:320px; background:#f5f5f5; border-radius:8px;"
                " display:flex; align-items:center; justify-content:center;"
                " color:#aaa; font-size:1.1em; border:1px dashed #ccc'>"
                "📷 Click a thumbnail below to bind a photo</div>",
                unsafe_allow_html=True,
            )

    with right:
        # ── editable Chinese name ─────────────────────────────────────────────
        st.text_input(
            "Chinese name (edit if wrong)",
            key=name_key,
            on_change=_on_name_change,
            args=(row_id,),
        )

        st.markdown("---")

        # ── dual price display ────────────────────────────────────────────────
        jhs = row.get("jihuanshe_price_cny")
        usd = row.get("usd_market")

        jhs_col, usd_col = st.columns(2)

        with jhs_col:
            jhs_active = price_source == "jhs"
            st.markdown("**Jihuanshe (CNY)**" + (" ✅" if jhs_active else ""))
            if jhs is not None:
                st.markdown(f"### ¥ {jhs:.2f}")
                st.caption("source: sbwsz")
            else:
                st.markdown("### N/A")
                st.caption("not in sbwsz")
            jhs_btn = "▶ Active" if jhs_active else "Use this ✓"
            if st.button(jhs_btn, key=f"use_jhs_{row_id}",
                         disabled=(jhs is None), use_container_width=True):
                _set_row_state(
                    state, row_id, price_cny=jhs, price_source="jhs", price_error=None
                )
                st.rerun()

        with usd_col:
            usd_active = price_source == "usd_converted"
            st.markdown("**USD market**" + (" ✅" if usd_active else ""))
            if usd is not None:
                st.markdown(f"### ${usd:.2f}")
                st.caption(f"× {FX_RATE} = ¥ {usd * FX_RATE:.2f}")
            else:
                st.markdown("### N/A")
                st.caption("no USD price")
            usd_btn = "▶ Active" if usd_active else "Use this ✓"
            if st.button(usd_btn, key=f"use_usd_{row_id}",
                         disabled=(usd is None), use_container_width=True):
                _set_row_state(
                    state,
                    row_id,
                    price_cny=round(usd * FX_RATE, 2),
                    price_source="usd_converted",
                    price_error=None,
                )
                st.rerun()

        st.markdown("---")

        # ── manual override ───────────────────────────────────────────────────
        manual_active = price_source == "manual"
        st.text_input(
            "Manual override (CNY)" + (" ✅" if manual_active else ""),
            key=price_key,
            placeholder="Enter CNY amount if neither above is right",
            on_change=_on_price_override_change,
            args=(row_id,),
        )

        # ── approval readiness hint ───────────────────────────────────────────
        if hints_for_approval:
            st.markdown("---")
            for msg, level in hints_for_approval:
                if level == "warning":
                    st.markdown(
                        f"<small style='color:#c05000'>{msg}</small>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption(msg)

        # ── action buttons ────────────────────────────────────────────────────
        st.markdown("---")

        # Approve — replaced by muted status on approved rows
        if current_row_state == "approved":
            approved_ts = row_entry.get("approved_at", "")
            approved_date = approved_ts[:10] if approved_ts else "?"
            st.markdown(
                f"<span style='color:#2d7d2d'>✓ Approved on {approved_date}</span>",
                unsafe_allow_html=True,
            )
            st.caption("To re-approve, see CLAUDE.md Operations section.")
            txt_path = LISTINGS_DIR / f"{row_id}.txt"
            if txt_path.exists():
                st.code(txt_path.read_text(encoding="utf-8"), language=None)
        else:
            approve_disabled = (
                current_row_state not in ("ready_to_review", "skipped") or blocking
            )
            if st.button(
                "✓ Approve",
                type="primary",
                disabled=approve_disabled,
                use_container_width=True,
                key=f"approve_{row_id}",
            ):
                _do_approve(row, row_id, state)
                next_idx = _next_unfinished_idx(display_rows, state, idx)
                if next_idx is not None:
                    st.session_state.current_idx = next_idx
                    st.session_state.pop("_all_caught_up", None)
                else:
                    st.session_state.current_idx = idx
                    st.session_state._all_caught_up = True
                st.session_state.pop("display_rows", None)
                st.rerun()

        # Skip — muted indicator for already-skipped; hint for waiting_photo; hidden for approved
        if current_row_state == "skipped":
            st.markdown(
                "<span style='color:#a06010'>⊘ Already skipped</span>",
                unsafe_allow_html=True,
            )
        elif current_row_state == "waiting_photo":
            st.caption("Bind a photo first to enable approval.")
        elif current_row_state == "ready_to_review":
            if st.button(
                "→ Skip",
                use_container_width=True,
                key=f"skip_{row_id}",
            ):
                _ensure_row(state, row_id)
                state["rows"][row_id]["state"] = "skipped"
                _save_state(state)
                next_idx = _next_unfinished_idx(display_rows, state, idx)
                if next_idx is not None:
                    st.session_state.current_idx = next_idx
                    st.session_state.pop("_all_caught_up", None)
                else:
                    st.session_state.current_idx = idx
                    st.session_state._all_caught_up = True
                st.session_state.pop("display_rows", None)
                st.rerun()

        # Back — always present, disabled at first row
        if st.button(
            "← Back",
            disabled=(idx == 0),
            use_container_width=True,
            key=f"back_{row_id}",
        ):
            st.session_state.current_idx = idx - 1
            st.session_state.pop("_all_caught_up", None)
            st.rerun()

        # "All caught up" only when auto-advance found no next ready_to_review row
        if st.session_state.get("_all_caught_up"):
            st.caption("All caught up for now — no more rows ready to review.")

    # ── thumbnail grid (full width) ───────────────────────────────────────────
    st.divider()
    pool = st.session_state.unbound_pool
    pool_hdr, pool_btn = st.columns([5, 1])
    with pool_hdr:
        st.markdown(f"**Unbound photos (pool: {len(pool)})**")
    with pool_btn:
        if st.button("↻ Refresh", key="refresh_pool", use_container_width=True):
            _refresh_pool(state)
            st.rerun()

    if not pool:
        st.caption("No unbound photos — add photos to data/mtg_photos/ and click ↻ Refresh.")
    else:
        page = st.session_state.get("thumbnail_page", 0)
        total_pages = max(1, (len(pool) + THUMBNAILS_PER_PAGE - 1) // THUMBNAILS_PER_PAGE)
        page = min(page, total_pages - 1)
        st.session_state.thumbnail_page = page

        start = page * THUMBNAILS_PER_PAGE
        visible = pool[start : start + THUMBNAILS_PER_PAGE]

        cols = st.columns(THUMBNAIL_COLS)
        for i, photo_path in enumerate(visible):
            with cols[i % THUMBNAIL_COLS]:
                try:
                    st.image(_load_thumbnail(str(photo_path)), width=150)
                except Exception:
                    st.markdown("⚠️ unreadable")
                st.caption(photo_path.name)
                if st.button("Bind ✓", key=f"bind_{photo_path.name}",
                             use_container_width=True):
                    _bind_photo(row_id, photo_path, state)

        # Pagination — four equal-width buttons in one row
        at_first = page == 0
        at_last  = page >= total_pages - 1
        pg_first, pg_prev, pg_next, pg_last = st.columns(4)
        with pg_first:
            if st.button("<< First", disabled=at_first, use_container_width=True):
                st.session_state.thumbnail_page = 0
                st.rerun()
        with pg_prev:
            if st.button("< Prev", disabled=at_first, use_container_width=True):
                st.session_state.thumbnail_page = page - 1
                st.rerun()
        with pg_next:
            if st.button("Next >", disabled=at_last, use_container_width=True):
                st.session_state.thumbnail_page = page + 1
                st.rerun()
        with pg_last:
            if st.button("Last >>", disabled=at_last, use_container_width=True):
                st.session_state.thumbnail_page = total_pages - 1
                st.rerun()


if __name__ == "__main__":
    _render_app()
