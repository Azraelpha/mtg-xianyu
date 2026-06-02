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

FX_RATE = 7.25  # CNY per USD — placeholder, tune after first sales


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
        st.checkbox(
            "Show all rows",
            value=False,
            disabled=True,
            key="show_all",
            help="State-aware filtering comes in Stage 3.",
        )

    # ── session state init / sort change ──────────────────────────────────────
    all_rows = _load_enriched()

    if not all_rows:
        st.error("data/enriched.json not found. Run mtg-enrich first.")
        return

    needs_init = "rows" not in st.session_state
    sort_changed = st.session_state.get("_applied_sort") != sort_by

    if needs_init or sort_changed:
        st.session_state.rows = sort_rows(all_rows, sort_by)
        st.session_state._applied_sort = sort_by
        st.session_state.current_idx = 0

    rows = st.session_state.rows
    total = len(rows)
    idx = st.session_state.current_idx
    row = rows[idx]

    # ── top bar ───────────────────────────────────────────────────────────────
    row_id    = row.get("row_id", str(idx))
    name_en   = row.get("name_en", "?")
    name_zh   = row.get("name_zh") or "?"
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
            value=row.get("name_zh") or "",
            key=f"name_zh_{row_id}",
        )

        st.markdown("---")

        # ── dual price display ────────────────────────────────────────────────
        jhs = row.get("jihuanshe_price_cny")
        usd = row.get("usd_market")

        jhs_col, usd_col = st.columns(2)

        with jhs_col:
            st.markdown("**Jihuanshe (CNY)**")
            if jhs is not None:
                st.markdown(f"### ¥ {jhs:.2f}")
                st.caption("source: sbwsz")
            else:
                st.markdown("### N/A")
                st.caption("not in sbwsz")
            if st.button("Use this ✓", key=f"use_jhs_{row_id}",
                         disabled=(jhs is None), use_container_width=True):
                st.toast("Price selection wired in Stage 5")

        with usd_col:
            st.markdown("**USD market**")
            if usd is not None:
                st.markdown(f"### ${usd:.2f}")
                st.caption(f"× {FX_RATE} = ¥ {usd * FX_RATE:.2f}")
            else:
                st.markdown("### N/A")
                st.caption("no USD price")
            if st.button("Use this ✓", key=f"use_usd_{row_id}",
                         disabled=(usd is None), use_container_width=True):
                st.toast("Price selection wired in Stage 5")

        st.markdown("---")

        # ── manual override ───────────────────────────────────────────────────
        st.text_input(
            "Manual override (CNY)",
            value="",
            key=f"price_override_{row_id}",
            placeholder="Enter CNY amount if neither above is right",
        )


if __name__ == "__main__":
    _render_app()
