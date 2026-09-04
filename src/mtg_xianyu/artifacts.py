"""Pure filename and path helpers for approved listing artifacts."""

import re
from pathlib import Path

from mtg_xianyu.describe import build_finish_zh


_TRAILING_PAREN_RE = re.compile(r"\s*\([^)]+\)\s*$")
_UNSAFE_FILENAME_CHARS_RE = re.compile(r"[/\\:\x00-\x1f\x7f]")


def safe_filename_component(value: object) -> str:
    """Return one normalized filename component with no path separators."""
    sanitized = _UNSAFE_FILENAME_CHARS_RE.sub("-", str(value or ""))
    return " ".join(sanitized.split()).strip(" .")


def safe_name_en(name_en: str) -> str:
    """Strip parentheticals and sanitize name_en for use in a filename."""
    value = name_en or ""
    while _TRAILING_PAREN_RE.search(value):
        value = _TRAILING_PAREN_RE.sub("", value).strip()
    return safe_filename_component(value)


def confined_listing_path(listings_dir: Path, filename: str) -> Path:
    """Build a direct child path and reject symlink/path traversal escapes."""
    if Path(filename).parent != Path("."):
        raise ValueError(f"listing filename escapes {listings_dir}: {filename!r}")
    candidate = listings_dir / filename
    if candidate.resolve().parent != listings_dir.resolve():
        raise ValueError(f"listing filename escapes {listings_dir}: {filename!r}")
    return candidate


def row_artifact_path(row_id: str, suffix: str, listings_dir: Path) -> Path:
    """Return a confined JSON or text artifact path for one row."""
    if suffix not in {".json", ".txt"}:
        raise ValueError(f"unsupported row artifact suffix: {suffix!r}")
    return confined_listing_path(listings_dir, f"{row_id}{suffix}")


def jpg_path_for(
    listing: dict,
    listings_dir: Path,
    current_path: Path | None = None,
    reserved_paths: set[Path] | None = None,
) -> Path:
    """Build the human-readable JPEG path, allowing its current occupied path."""
    row_id = safe_filename_component(listing.get("row_id")) or "unknown"
    name_safe = safe_name_en(listing.get("name_en", ""))

    if not name_safe:
        return confined_listing_path(listings_dir, f"{row_id}.jpg")

    finish_zh = safe_filename_component(
        build_finish_zh(listing["name_en"], listing["printing"])
    )
    set_safe = safe_filename_component(listing.get("set_code")) or "UNKNOWN"
    number_safe = (
        safe_filename_component(listing.get("collector_number")) or "unknown"
    )
    stem = f"{set_safe}-{number_safe} - {name_safe} - {finish_zh}"
    candidate = confined_listing_path(listings_dir, f"{stem}.jpg")
    copy_number = 1
    current_resolved = current_path.resolve() if current_path is not None else None
    reserved_resolved = {path.resolve() for path in (reserved_paths or set())}
    while (
        candidate.exists() and candidate.resolve() != current_resolved
    ) or candidate.resolve() in reserved_resolved:
        candidate = confined_listing_path(
            listings_dir,
            f"{stem} (copy {copy_number}).jpg",
        )
        copy_number += 1
    return candidate
