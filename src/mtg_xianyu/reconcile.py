"""Dry-run-first reconciliation for approved listing descriptions and JPEG names."""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from mtg_xianyu.artifacts import jpg_path_for, row_artifact_path
from mtg_xianyu.describe import build_description
from mtg_xianyu.storage import atomic_write_json, atomic_write_text


@dataclass(frozen=True)
class ListingUpdate:
    """One approved listing whose generated artifacts no longer match the code."""

    row_id: str
    listing: dict
    json_path: Path
    current_jpg: Path
    desired_jpg: Path
    txt_path: Path
    description: str
    rename_jpg: bool
    write_text: bool


def _load_listing(json_path: Path) -> dict:
    try:
        listing = json.loads(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load approved listing {json_path}: {exc}") from exc
    if not isinstance(listing, dict):
        raise ValueError(f"invalid approved listing {json_path}: expected an object")
    row_id = listing.get("row_id")
    if row_id != json_path.stem:
        raise ValueError(
            f"invalid approved listing {json_path}: row_id {row_id!r} "
            f"does not match filename {json_path.stem!r}"
        )
    return listing


def _current_jpg_path(listing: dict, json_path: Path, listings_dir: Path) -> Path:
    raw_path = listing.get("photo_jpg")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError(
            f"invalid approved listing {json_path}: 'photo_jpg' must be a path"
        )
    path = Path(raw_path)
    if path.resolve().parent != listings_dir.resolve():
        raise ValueError(
            f"invalid approved listing {json_path}: photo_jpg escapes "
            f"{listings_dir}: {raw_path!r}"
        )
    if not path.is_file():
        raise ValueError(
            f"invalid approved listing {json_path}: photo_jpg does not exist: "
            f"{raw_path!r}"
        )
    return path


def plan_listing_updates(listings_dir: Path) -> list[ListingUpdate]:
    """Return every description or JPEG name that differs from current logic."""
    if not listings_dir.is_dir():
        raise ValueError(f"listing directory does not exist: {listings_dir}")
    updates: list[ListingUpdate] = []
    reserved_jpg_paths: set[Path] = set()
    json_files = sorted(
        path for path in listings_dir.glob("*.json") if path.name != "state.json"
    )
    for json_path in json_files:
        listing = _load_listing(json_path)
        row_id = listing["row_id"]
        current_jpg = _current_jpg_path(listing, json_path, listings_dir)
        desired_jpg = jpg_path_for(
            listing,
            listings_dir=listings_dir,
            current_path=current_jpg,
            reserved_paths=reserved_jpg_paths,
        )
        reserved_jpg_paths.add(desired_jpg)
        txt_path = row_artifact_path(row_id, ".txt", listings_dir)
        description = build_description(listing)
        try:
            current_description = txt_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            current_description = None
        except OSError as exc:
            raise ValueError(
                f"cannot read listing description {txt_path}: {exc}"
            ) from exc

        rename_jpg = current_jpg.resolve() != desired_jpg.resolve()
        write_text = current_description != description
        if rename_jpg or write_text:
            updates.append(
                ListingUpdate(
                    row_id=row_id,
                    listing=listing,
                    json_path=json_path,
                    current_jpg=current_jpg,
                    desired_jpg=desired_jpg,
                    txt_path=txt_path,
                    description=description,
                    rename_jpg=rename_jpg,
                    write_text=write_text,
                )
            )
    return updates


def apply_listing_update(update: ListingUpdate) -> None:
    """Apply one planned update, rolling back a rename if JSON persistence fails."""
    if update.rename_jpg:
        if update.desired_jpg.exists():
            raise FileExistsError(
                f"refusing to replace existing JPEG {update.desired_jpg}"
            )
        update.current_jpg.rename(update.desired_jpg)
        revised_listing = {**update.listing, "photo_jpg": str(update.desired_jpg)}
        try:
            atomic_write_json(update.json_path, revised_listing)
        except Exception as exc:
            try:
                update.desired_jpg.rename(update.current_jpg)
            except OSError as rollback_exc:
                raise RuntimeError(
                    f"JSON update failed for {update.row_id}, and JPEG rollback "
                    f"also failed: {rollback_exc}"
                ) from exc
            raise

    if update.write_text:
        atomic_write_text(update.txt_path, update.description)


def _print_plan(updates: list[ListingUpdate], *, apply: bool) -> None:
    verb = "Applying" if apply else "Would apply"
    for update in updates:
        if update.rename_jpg:
            print(
                f"{verb} JPEG rename for {update.row_id}: "
                f"{update.current_jpg.name} -> {update.desired_jpg.name}"
            )
        if update.write_text:
            print(
                f"{verb} description update for {update.row_id}: "
                f"{update.txt_path.name}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reconcile approved listing descriptions and JPEG filenames."
    )
    parser.add_argument(
        "--listings-dir",
        type=Path,
        default=Path("data/listings"),
        help="Approved listing directory (default: data/listings)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the displayed updates. Without this flag, runs dry-run.",
    )
    args = parser.parse_args()

    updates = plan_listing_updates(args.listings_dir)
    if not updates:
        print("All approved listing artifacts already match current generation logic.")
        return

    if not args.apply:
        print("DRY RUN — no listing artifacts will be changed.")
    _print_plan(updates, apply=args.apply)
    if args.apply:
        for update in updates:
            apply_listing_update(update)

    rename_count = sum(update.rename_jpg for update in updates)
    text_count = sum(update.write_text for update in updates)
    action = "Applied" if args.apply else "Planned"
    print(
        f"{action} {rename_count} JPEG rename(s) and "
        f"{text_count} description update(s)."
    )


if __name__ == "__main__":
    main()
