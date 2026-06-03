"""Streamlit review UI for MTG → Xianyu listings.

Loads data/enriched.json, sorts by effective CNY price descending, and
displays one card at a time for photo binding and approval.

Run with:
    uv run streamlit run src/mtg_xianyu/ui.py

Must be run from the project root so that relative data/ paths resolve.
"""

import json
from pathlib import Path

import streamlit as st

FX_RATE = 7.25          # CNY per USD — placeholder, tune after first sales
STATE_PATH = Path("data/listings/state.json")


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

    # ── rebuild display list on sort or filter change ─────────────────────────
    needs_rebuild = (
        "display_rows" not in st.session_state
        or st.session_state.get("_applied_sort") != sort_by
        or st.session_state.get("_applied_show_all") != show_all
    )
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
    # Header reflects any in-session or persisted name override
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
        st.markdown(
            "<div style='height:320px; background:#f5f5f5; border-radius:8px;"
            " display:flex; align-items:center; justify-content:center;"
            " color:#aaa; font-size:1.1em; border:1px dashed #ccc'>"
            "📷 Photo binding — Stage 4</div>",
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


if __name__ == "__main__":
    _render_app()
