"""Enrich rows with Chinese metadata from sbwsz.com (大学院废墟).

Bootstraps the sbwsz set list once (cached weekly under
data/cache/sbwsz/sets.json), resolves each row's set code via exact then
fuzzy match, and calls get_card_by_set_and_number per card (responses cached
under data/cache/sbwsz/cards/{set_code}/{number}.json). Adds name_zh,
set_code, set_name_zh, sbwsz_image_uri, and jihuanshe_price_cny to each row.
Missing Chinese names are left null — never invented. Writes enriched.json.
"""


def main() -> None:
    raise NotImplementedError()
