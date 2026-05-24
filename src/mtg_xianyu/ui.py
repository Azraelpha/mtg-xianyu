"""Streamlit review and edit UI for generated listings.

Displays one card per page. Left panel: user photo and sbwsz reference image
side by side. Right panel: all enriched fields (editable), a 2×2 pricing grid
(USD-derived vs Jihuanshe-derived × fast-sell vs hold-out) as radio buttons
with a free-text override, and the generated description in a textarea.
Actions: Approve, Skip, Flag for follow-up. Approved rows append to
listings.json; state is persisted so sessions are resumable across browser
tab closes. On approval, writes a JPEG copy of the HEIC photo at quality 90.

Run with: streamlit run src/mtg_xianyu/ui.py
"""


def main() -> None:
    raise NotImplementedError()
