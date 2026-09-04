"""Generate Xianyu listing descriptions from approved listing dicts.

Template-driven, no LLM calls. Treatment classification covers:
  - VISUAL_TREATMENTS combine with base finish  (e.g. Borderless → 异画英文闪)
  - FINISH_VARIANTS   replace the base finish   (e.g. Foil Etched → 英文蚀刻闪)
  - LANGUAGE_VARIANTS replace the default English label
  - edition-only suffixes are stripped without changing the finish

CLI: reads data/listings/*.json, writes {row_id}.txt, prints unknown-suffix tally.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

from mtg_xianyu.storage import atomic_write_text

SHOP_POLICY = "主页满300包邮"

VISUAL_TREATMENTS: dict[str, str] = {
    "Anime Borderless": "动漫无边框",
    "Borderless":       "异画",
    "Extended Art":     "扩画",
    "Future Sight":     "未来框",
    "Retro Frame":      "老框",
    "Showcase":         "异画",
    "White Border":     "白边",
}

FINISH_VARIANTS: dict[str, str] = {
    "Foil Etched":           "蚀刻闪",
    "Oil Slick Raised Foil": "油膜浮雕闪",
    "Rainbow Foil":          "彩虹闪",
    "Surge Foil":            "潮涌闪",
}

LANGUAGE_VARIANTS: dict[str, str] = {
    "JP Alternate Art": "日文异画",
    "Phyrexian":        "非瑞克西亚文",
}

IGNORED_EDITION_SUFFIXES = {"Post Malone"}

DEFAULT_LANGUAGE = "英文"
BASE_FINISH: dict[str, str] = {"Foil": "闪", "Normal": "平"}

_SET_CODE_RE = re.compile(r"^[A-Z0-9]{2,4}$")
_NUMBER_RE   = re.compile(r"^\d+$")
_BUNDLE_RE   = re.compile(r"^[A-Z0-9]{2,4} Bundle$")


def _is_known_suffix(suffix: str) -> bool:
    """Return whether suffix has an explicit treatment or metadata meaning."""
    return bool(
        suffix in VISUAL_TREATMENTS
        or suffix in FINISH_VARIANTS
        or suffix in LANGUAGE_VARIANTS
        or suffix in IGNORED_EDITION_SUFFIXES
        or _SET_CODE_RE.fullmatch(suffix)
        or _NUMBER_RE.fullmatch(suffix)
        or _BUNDLE_RE.fullmatch(suffix)
    )


def _extract_suffixes(name_en: str) -> tuple[str, list[str]]:
    """Strip trailing parentheticals iteratively.

    Returns (base_name, suffixes) ordered innermost → outermost.
    """
    suffixes: list[str] = []
    while m := re.search(r"\s*\(([^)]+)\)\s*$", name_en):
        suffixes.append(m.group(1))
        name_en = name_en[: m.start()].strip()
    return name_en, suffixes


def _classify_and_compose_finish(
    suffixes: list[str], printing: str, card_ref: str
) -> str:
    """Classify parenthetical suffixes and compose the finish_zh label."""
    visual_prefix = ""
    finish_variant: str | None = None
    language = DEFAULT_LANGUAGE

    for s in suffixes:
        if s in VISUAL_TREATMENTS:
            if not visual_prefix:           # first visual treatment wins
                visual_prefix = VISUAL_TREATMENTS[s]
        elif s in FINISH_VARIANTS:
            if printing != "Foil":
                print(
                    f"[describe] {card_ref}: finish variant '{s}' but "
                    f"printing='{printing}' — trusting suffix",
                    file=sys.stderr,
                )
            finish_variant = FINISH_VARIANTS[s]
        elif s in LANGUAGE_VARIANTS:
            language = LANGUAGE_VARIANTS[s]
        elif _is_known_suffix(s):
            pass  # silently strip set codes, numbers, bundles, and edition tags
        else:
            print(
                f"[describe] {card_ref}: unknown suffix '{s}' — "
                f"defaulting to no treatment",
                file=sys.stderr,
            )

    if finish_variant:
        return f"{visual_prefix}{language}{finish_variant}"
    return f"{visual_prefix}{language}{BASE_FINISH[printing]}"


def build_finish_zh(name_en: str, printing: str) -> str:
    """Return the finish_zh label (e.g. '异画英文平') for a card.

    Shared by describe.py (description line 3) and ui.py (JPEG filename).
    """
    _, suffixes = _extract_suffixes(name_en)
    return _classify_and_compose_finish(suffixes, printing, card_ref=name_en)


def build_description(listing: dict) -> str:
    """Return the 4-line Xianyu listing description for one approved card."""
    name_en_clean, _ = _extract_suffixes(listing["name_en"])
    finish_zh = build_finish_zh(listing["name_en"], listing["printing"])
    return "\n".join([
        f"万智牌 MTG {listing['name_zh']}",
        name_en_clean,
        f"{listing['set_name_zh']}/{listing['set_code']} {finish_zh}",
        SHOP_POLICY,
    ])


def main() -> None:
    listings_dir = Path("data/listings")
    listing_files = sorted(
        p for p in listings_dir.glob("*.json") if p.name != "state.json"
    )

    if not listing_files:
        print("No listing JSON files found in data/listings/.", file=sys.stderr)
        return

    unknown_tally: dict[str, list[str]] = defaultdict(list)
    written = 0

    for path in listing_files:
        listing = json.loads(path.read_text(encoding="utf-8"))
        desc = build_description(listing)
        atomic_write_text(path.with_suffix(".txt"), desc)
        written += 1

        _, suffixes = _extract_suffixes(listing["name_en"])
        for s in suffixes:
            if not _is_known_suffix(s):
                unknown_tally[s].append(listing["name_en"])

    print(f"Wrote {written} description(s).")
    if unknown_tally:
        print("\nUnknown suffixes (need mapping or investigation):")
        for suffix, cards in sorted(unknown_tally.items()):
            print(f"  '{suffix}': {', '.join(cards)}")
    else:
        print("No unknown suffixes — all treatments classified.")
