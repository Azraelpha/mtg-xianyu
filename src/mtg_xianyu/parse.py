"""Parse a TCGPlayer collection export (.numbers or .csv) into normalized rows.

Reads the export, normalises column names, and expands any row where
Add to Quantity > 1 into one logical row per physical card. Downstream
stages must never see multi-quantity rows. Writes rows.json.
"""


def main() -> None:
    raise NotImplementedError()
