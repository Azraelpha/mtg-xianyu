#!/usr/bin/env python3
"""One-off backfill: rename legacy {row_id}.jpg listing files to the
human-readable {set_code}-{number} - {name} - {finish_zh}.jpg scheme
introduced alongside the approve-action rename feature.

Listings approved before that feature landed keep their opaque row_id
names on disk; this script brings them in line without modifying any
other content.

Run from project root:
    uv run python scripts/rename_legacy_listings.py          # dry-run (safe)
    uv run python scripts/rename_legacy_listings.py --apply  # actually rename

The --apply flag is required to make any changes. Without it the script
prints every action it would take and exits cleanly.
"""

import argparse
import json
import sys
from pathlib import Path

# Ensure the package is importable when running as a standalone script.
# `uv run` sets up the venv automatically, so this is a belt-and-suspenders
# fallback for environments where the package isn't on sys.path yet.
_src = Path(__file__).parent.parent / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from mtg_xianyu.storage import atomic_write_json  # noqa: E402
from mtg_xianyu.ui import _jpg_path_for  # noqa: E402

LISTINGS_DIR = Path("data/listings")


def _is_legacy(json_path: Path, listing: dict) -> bool:
    """True if the listing's photo_jpg still uses the {row_id}.jpg scheme."""
    current_name = Path(listing.get("photo_jpg", "")).name
    return current_name == f"{json_path.stem}.jpg"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rename legacy listing JPEGs to human-readable filenames."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually rename files and update JSON. Without this flag, runs dry-run.",
    )
    args = parser.parse_args()
    dry_run = not args.apply

    if dry_run:
        print("DRY RUN — pass --apply to perform renames. Nothing will be changed.\n")
    else:
        print("APPLYING renames...\n")

    json_files = sorted(
        p for p in LISTINGS_DIR.glob("*.json") if p.name != "state.json"
    )

    n_checked = n_skipped = n_renamed = n_errors = 0
    proposed: dict[str, str] = {}  # new_name → row_id; for dry-run collision detection

    for json_path in json_files:
        n_checked += 1
        row_id = json_path.stem

        try:
            listing = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"ERROR  {row_id}: could not read JSON — {exc}")
            n_errors += 1
            continue

        if not _is_legacy(json_path, listing):
            n_skipped += 1
            continue

        legacy_name = f"{row_id}.jpg"
        legacy_path = LISTINGS_DIR / legacy_name

        if not legacy_path.exists():
            print(f"WARN   {row_id}: {legacy_name} not found on disk — skipping")
            n_errors += 1
            continue

        try:
            new_path = _jpg_path_for(listing)
        except Exception as exc:
            print(f"ERROR  {row_id}: could not compute new filename — {exc}")
            n_errors += 1
            continue

        new_name = new_path.name

        # In dry-run mode the filesystem doesn't change, so _jpg_path_for can't
        # detect collisions via .exists(). Track proposed names ourselves.
        if dry_run and new_name in proposed:
            first = proposed[new_name]
            print(
                f"COLLISION {row_id}: '{new_name}' already proposed by {first} "
                f"— would need further disambiguation"
            )
        proposed[new_name] = row_id

        verb = "would rename" if dry_run else "renamed  "
        print(f"{verb}: {legacy_name}  →  {new_name}")

        if dry_run:
            n_renamed += 1
            continue

        # ── apply ────────────────────────────────────────────────────────────
        try:
            legacy_path.rename(new_path)
        except Exception as exc:
            print(f"ERROR  {row_id}: rename failed — {exc}")
            n_errors += 1
            continue

        try:
            listing["photo_jpg"] = str(new_path)
            atomic_write_json(json_path, listing)
        except Exception as exc:
            # File is already renamed; record the inconsistency so the user
            # knows which JSON needs a manual photo_jpg fix.
            print(f"ERROR  {row_id}: JSON update failed after rename — {exc}")
            print(f"       Manual fix: set photo_jpg to \"{new_path}\" in {json_path.name}")
            n_errors += 1
            continue

        n_renamed += 1

    action = "would be renamed" if dry_run else "renamed"
    print(
        f"\nSummary: {n_checked} checked, "
        f"{n_skipped} already in new format (skipped), "
        f"{n_renamed} {action}, "
        f"{n_errors} errors."
    )


if __name__ == "__main__":
    main()
