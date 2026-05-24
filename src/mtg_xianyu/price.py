"""Compute two independent price suggestions per card.

Source A (always available): USD heuristic derived from TCGPlayer market price
converted at a configurable fx_rate with fast-sell (×0.55) and hold-out (×1.0)
multipliers. Source B (when jihuanshe_price_cny is present): Jihuanshe-derived
fast-sell (×0.60) and hold-out (×0.90) figures. Both sources are always written
to the output so the UI can show both; Jihuanshe fields are null when the source
price was null. Writes priced.json.
"""


def main() -> None:
    raise NotImplementedError()
