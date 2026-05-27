"""Enrich parsed rows with Chinese metadata from sbwsz.com (大学院废墟).

Bootstraps the sbwsz set list once (cached weekly under
data/cache/sbwsz/sets.json), resolves each row's set code via exact then
fuzzy match, and calls get_card_by_set_and_number per card (responses cached
under data/cache/sbwsz/cards/{set_code}/{number}.json). Adds name_zh,
set_code, set_name_zh, sbwsz_image_uri, and jihuanshe_price_cny to each row.
Missing Chinese names are left null — never invented. Writes enriched.json.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

BASE_URL = "https://mtgch.com/api/v1"
USER_AGENT = "mtg-xianyu/0.1 (https://github.com/Azraelpha/mtg-xianyu)"
SETS_TTL = 7 * 24 * 3600   # seconds before the set list is re-fetched
RATE_DELAY = 0.5            # minimum seconds between live requests

_last_request_at: float = 0.0


# ── cache helpers ─────────────────────────────────────────────────────────────

def _cache_path(kind: str, *parts: str) -> Path:
    base = Path("data/cache/sbwsz")
    if parts:
        return base / kind / Path(*parts).with_suffix(".json")
    return base / f"{kind}.json"


def _read_cache(path: Path) -> dict | list | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, data: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _fetch(client: httpx.Client, url: str, *, _retries: int = 3) -> dict | list:
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if _last_request_at > 0 and elapsed < RATE_DELAY:
        time.sleep(RATE_DELAY - elapsed)
    _last_request_at = time.monotonic()
    r = client.get(url)
    if r.status_code == 429 and _retries > 0:
        wait = float(r.headers.get("Retry-After", 10))
        print(f"\n[429] rate limited; waiting {wait:.0f}s …", file=sys.stderr)
        time.sleep(wait)
        return _fetch(client, url, _retries=_retries - 1)
    r.raise_for_status()
    return r.json()


def _get_sets(client: httpx.Client) -> list[dict]:
    path = _cache_path("sets")
    cached = _read_cache(path)
    if cached is not None:
        if time.time() - cached.get("fetched_at", 0) < SETS_TTL:
            return cached["data"]
    data = _fetch(client, f"{BASE_URL}/sets/")
    _write_cache(path, {"fetched_at": time.time(), "data": data})
    return data


def _get_card(client: httpx.Client, set_code: str, number: str) -> dict:
    # Always ?view=1 — bare endpoint omits prices.cny and versions array.
    # Cache key corresponds to the full URL (path + query string).
    url = f"{BASE_URL}/card/{set_code}/{number}/?view=1"
    path = _cache_path("cards", set_code, number)
    cached = _read_cache(path)
    if cached is not None:
        return cached
    try:
        data = _fetch(client, url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            print(f"[404] {set_code}/{number} not in sbwsz — enrichment fields will be null", file=sys.stderr)
            return {}
        raise
    _write_cache(path, data)
    return data


# ── set resolution ────────────────────────────────────────────────────────────

def _build_set_dicts(
    sets_data: list[dict],
) -> tuple[dict[str, str], dict[str, str | None]]:
    en_to_code = {e["name"]: e["code"] for e in sets_data}
    code_to_zh = {e["code"]: e.get("translated_name") for e in sets_data}
    return en_to_code, code_to_zh


def _resolve_set_code(
    set_name_en: str,
    en_to_code: dict[str, str],
    row_ref: str,
    fuzzy_log: list | None = None,
) -> str:
    if set_name_en in en_to_code:
        return en_to_code[set_name_en]
    result = process.extractOne(
        set_name_en, en_to_code.keys(), scorer=fuzz.WRatio, score_cutoff=80
    )
    if result is not None:
        matched_name, score, _ = result
        print(f"[fuzzy] {set_name_en!r} → {matched_name!r} ({score:.0f})", file=sys.stderr)
        if fuzzy_log is not None:
            fuzzy_log.append((set_name_en, matched_name, score))
        return en_to_code[matched_name]
    raise ValueError(f"no set_code for {set_name_en!r} in {row_ref}")


# ── field extraction ──────────────────────────────────────────────────────────

def _name_zh(card: dict) -> str | None:
    name = card.get("primary_name")
    if not name:
        return None
    name_source = (card.get("translation_info") or {}).get("name_source", "")
    if name_source and name_source != "官方中文":
        print(
            f"[name_zh] community translation used: {name!r} (source: {name_source!r})",
            file=sys.stderr,
        )
    return name


def _jihuanshe_price(card: dict) -> float | None:
    raw = card.get("prices", {}).get("cny")
    if raw is None or raw == "":
        return None
    return float(raw)


# ── enrichment ────────────────────────────────────────────────────────────────

def _enrich_row(
    idx: int,
    row: dict,
    en_to_code: dict[str, str],
    code_to_zh: dict[str, str | None],
    client: httpx.Client,
    *,
    stats: dict | None = None,
    fuzzy_log: list | None = None,
) -> dict:
    ref = f"source row {idx} ({row.get('name_en', '?')!r})"
    try:
        set_code = _resolve_set_code(row["set_name_en"], en_to_code, ref, fuzzy_log)
    except ValueError:
        print(f"[no-set] {row['set_name_en']!r} not in sbwsz — enrichment fields will be null", file=sys.stderr)
        if stats is not None:
            stats["no_set"] += 1
            stats["jhs_null"] += 1
        return {**row, "name_zh": None, "set_code": None, "set_name_zh": None,
                "sbwsz_image_uri": None, "jihuanshe_price_cny": None}

    card = _get_card(client, set_code, row["collector_number"])

    jhs = _jihuanshe_price(card)
    name_source = (card.get("translation_info") or {}).get("name_source", "")
    has_name = bool(card.get("primary_name"))

    if stats is not None:
        if not card:
            stats["not_found"] += 1
        elif has_name and name_source == "官方中文":
            stats["official"] += 1
        elif has_name:
            stats["community"] += 1
        else:
            stats["null_name"] += 1
        if jhs is not None:
            stats["jhs_populated"] += 1
        else:
            stats["jhs_null"] += 1

    face = (card.get("faces") or [{}])[0]
    image_uri = (face.get("zhs_image_uris") or face.get("image_uris") or {}).get("normal")

    return {
        **row,
        "name_zh": _name_zh(card),
        "set_code": set_code,
        "set_name_zh": code_to_zh.get(set_code),
        "sbwsz_image_uri": image_uri,
        "jihuanshe_price_cny": jhs,
    }


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich rows.json with sbwsz data.")
    parser.add_argument("input", nargs="?", default="data/rows.json")
    parser.add_argument("-o", "--output", default="data/enriched.json")
    args = parser.parse_args()

    rows: list[dict] = json.loads(Path(args.input).read_text(encoding="utf-8"))
    total = len(rows)

    client = httpx.Client(headers={"User-Agent": USER_AGENT})
    en_to_code, code_to_zh = _build_set_dicts(_get_sets(client))

    stats: dict[str, int] = {
        "official": 0, "community": 0, "null_name": 0, "not_found": 0, "no_set": 0,
        "jhs_populated": 0, "jhs_null": 0,
    }
    fuzzy_log: list[tuple[str, str, float]] = []
    results: list[dict] = []

    for idx, row in enumerate(rows):
        print(f"\r  enriching {idx + 1}/{total} …", end="", flush=True, file=sys.stderr)
        results.append(
            _enrich_row(idx, row, en_to_code, code_to_zh, client,
                        stats=stats, fuzzy_log=fuzzy_log)
        )
    print(file=sys.stderr)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {total} rows → {out}")

    print(f"\nname_zh outcomes ({total} rows):")
    print(f"  official (primary_name, 官方中文):  {stats['official']:>4}")
    print(f"  community (primary_name, other src):{stats['community']:>4}")
    print(f"  null (primary_name absent):         {stats['null_name']:>4}")
    print(f"  not found in sbwsz (404):           {stats['not_found']:>4}")
    print(f"  set not in sbwsz:                   {stats['no_set']:>4}")

    print(f"\njihuanshe_price_cny coverage:")
    print(f"  populated:  {stats['jhs_populated']:>4}")
    print(f"  null:       {stats['jhs_null']:>4}")

    print(f"\nfuzzy set matches: {len(fuzzy_log)}")
    for inp, match, score in fuzzy_log:
        print(f"  {inp!r} → {match!r} ({score:.0f})")


if __name__ == "__main__":
    main()
