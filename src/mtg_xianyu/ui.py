"""Public Streamlit UI module with review-fix safety patches.

The v0.5 implementation is preserved in ``_ui_impl.py``.  This wrapper keeps
that behavior while fixing filtered-view auto-advance and blocking approval for
rows whose set identity could not be resolved safely.
"""

from mtg_xianyu import _ui_impl as _impl

# Re-export implementation surface, including private helpers used by tests and
# the one-off rename script.
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

_original_approval_hints = _impl._approval_hints


def _approval_hints(row: dict, row_entry: dict) -> list[tuple[str, str]]:
    """Add unresolved-set identity to the existing approval gate."""
    hints = _original_approval_hints(row, row_entry)
    # Production enriched rows always contain set_code.  Treat an explicit
    # null as unresolved while keeping the helper usable with minimal test or
    # integration dictionaries that predate this field.
    if "set_code" in row and not row.get("set_code"):
        hints = [(msg, level) for msg, level in hints if level != "success"]
        hints.append(("⚠ Set identity is unresolved; fix enrichment before approving", "warning"))
    return hints


_impl._approval_hints = _approval_hints


def _next_unfinished_idx(
    display_rows: list[dict], state: dict, current_idx: int
) -> int | None:
    """Return the next ready row's index *after* applying the active view filter.

    The old implementation returned an index into the pre-mutation list.  When
    Approve/Skip removed the current row from a filtered view, that stale index
    pointed one row too far after the next rerun.
    """
    next_row_id = None
    for i in range(current_idx + 1, len(display_rows)):
        row_id = display_rows[i].get("row_id", "")
        if _impl._row_state(state, row_id) == "ready_to_review":
            next_row_id = row_id
            break
    if next_row_id is None:
        return None

    try:
        view = _impl.st.session_state.get("_applied_view", "All rows")
    except Exception:
        view = "All rows"

    if view not in _impl._VIEW_FILTERS:
        view = "All rows"
    rebuilt = _impl._filter_rows(display_rows, state, view)
    for i, row in enumerate(rebuilt):
        if row.get("row_id") == next_row_id:
            return i
    return None


_impl._next_unfinished_idx = _next_unfinished_idx

globals().update({
    "_approval_hints": _approval_hints,
    "_next_unfinished_idx": _next_unfinished_idx,
})

_render_app = _impl._render_app

if __name__ == "__main__":
    _render_app()
