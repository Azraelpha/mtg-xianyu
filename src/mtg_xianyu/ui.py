"""Streamlit review UI for MTG → Xianyu listings.

Loads data/enriched.json, sorts by effective CNY price descending, and
displays one card at a time for photo binding and approval.

Run with:
    uv run streamlit run src/mtg_xianyu/ui.py

Must be run from the project root so that relative data/ paths resolve.
"""

import io
import json
from pathlib import Path

from PIL import Image
from pillow_heif import register_heif_opener
import streamlit as st

register_heif_opener()  # enable HEIC support for PIL.Image.open()

FX_RATE = 7.25          # CNY per USD — placeholder, tune after first sales
STATE_PATH = Path("data/listings/state.json")
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


# ── state I/O ─────────────────────────────────────────────────────────────────

def _load_state() -> dict:
    """Read state.json. Returns empty structure if file doesn't exist."""
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"fx_rate": FX_RATE, "rows": {}}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def _ensure_row(state: dict, row_id: str) -> None:
    """Add a default entry for row_id if it isn't already in state."""
    if row_id not in state.setdefault("rows", {}):
        state["rows"][row_id] = {
            "state": "waiting_photo",
            "photo_path": None,
            "name_zh_override": None,
            "price_cny": None,
            "price_source": None,
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


# ── on_change callbacks ───────────────────────────────────────────────────────

def _on_name_change(row_id: str) -> None:
    val = st.session_state.get(f"name_zh_{row_id}", "").strip()
    state = st.session_state.state
    _ensure_row(state, row_id)
    state["rows"][row_id]["name_zh_override"] = val if val else None
    _save_state(state)
    st.rerun()


def _on_price_override_change(row_id: str) -> None:
    val = st.session_state.get(f"price_override_{row_id}", "").strip()
    state = st.session_state.state
    _ensure_row(state, row_id)
    if val:
        try:
            state["rows"][row_id]["price_cny"] = float(val)
            state["rows"][row_id]["price_source"] = "manual"
        except ValueError:
            pass  # keep existing state if input isn't a valid number
    elif state["rows"][row_id].get("price_source") == "manual":
        state["rows"][row_id]["price_cny"] = None
        state["rows"][row_id]["price_source"] = None
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

    if price_cny is None:
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

    if not hints:
        hints.append(("✓ Ready to approve", "success"))

    return hints


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
        show_all = st.checkbox("Show all rows", key="show_all")
        st.divider()
        if st.button("↻ Refresh photo pool", use_container_width=True):
            _refresh_pool(st.session_state.state if "state" in st.session_state else _load_state())
            st.rerun()

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

    # ── init photo pool (once per session) ───────────────────────────────────
    if "unbound_pool" not in st.session_state:
        st.session_state.unbound_pool = _build_unbound_pool(state)
        st.session_state.thumbnail_page = 0

    # ── rebuild display list ──────────────────────────────────────────────────
    # Rebuild when sort/filter changes (→ reset index) OR when a bind/unbind
    # invalidated the cache (→ preserve index).
    sort_changed = st.session_state.get("_applied_sort") != sort_by
    filter_changed = st.session_state.get("_applied_show_all") != show_all
    needs_rebuild = "display_rows" not in st.session_state or sort_changed or filter_changed

    if needs_rebuild:
        sorted_all = sort_rows(all_rows, sort_by)
        if show_all:
            display_rows = sorted_all
        else:
            display_rows = [
                r for r in sorted_all
                if _row_state(state, r.get("row_id", "")) in ("ready_to_review", "approved")
            ]
        st.session_state.display_rows = display_rows
        st.session_state._applied_sort = sort_by
        st.session_state._applied_show_all = show_all
        if sort_changed or filter_changed:
            st.session_state.current_idx = 0

    if "current_idx" not in st.session_state:
        st.session_state.current_idx = 0

    display_rows = st.session_state.display_rows
    total = len(display_rows)

    # ── empty state ───────────────────────────────────────────────────────────
    if total == 0:
        st.markdown("---")
        if not show_all:
            st.info(
                "All rows are waiting on photo binding — no reviews ready yet.\n\n"
                "Toggle **Show all rows** in the sidebar to browse the full collection."
            )
        else:
            st.error("No rows found in data/enriched.json.")
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

    st.markdown(f"## {name_en} — {name_zh}")
    st.caption(f"{set_label} · #{cn} · {condition} · {foil_badge} · {rarity}")
    st.divider()

    # ── middle row ────────────────────────────────────────────────────────────
    left, right = st.columns([3, 2])

    with left:
        st.caption(f"Row {idx + 1} of {total}")

        nav_prev, nav_next = st.columns(2)
        with nav_prev:
            if st.button("← Prev", disabled=(idx == 0), use_container_width=True):
                st.session_state.current_idx = idx - 1
                st.rerun()
        with nav_next:
            if st.button("Next →", disabled=(idx == total - 1), use_container_width=True):
                st.session_state.current_idx = idx + 1
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
                _set_row_state(state, row_id, price_cny=jhs, price_source="jhs")
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
                _set_row_state(state, row_id, price_cny=round(usd * FX_RATE, 2),
                               price_source="usd_converted")
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
        if row_entry.get("state") == "ready_to_review":
            st.markdown("---")
            for msg, level in _approval_hints(row, row_entry):
                if level == "warning":
                    st.markdown(
                        f"<small style='color:#c05000'>{msg}</small>",
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption(msg)

    # ── thumbnail grid (full width) ───────────────────────────────────────────
    st.divider()
    pool = st.session_state.unbound_pool
    st.markdown(f"**Unbound photos (pool: {len(pool)})**")

    if not pool:
        st.caption("No unbound photos. Add photos to data/mtg_photos/ and click ↻ Refresh.")
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

        # Pagination
        pg_prev, pg_next = st.columns(2)
        with pg_prev:
            if st.button("< Prev page", disabled=(page == 0), use_container_width=True):
                st.session_state.thumbnail_page = page - 1
                st.rerun()
        with pg_next:
            if st.button("Next page >", disabled=(page >= total_pages - 1),
                         use_container_width=True):
                st.session_state.thumbnail_page = page + 1
                st.rerun()


if __name__ == "__main__":
    _render_app()
