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
import re
import sys
import time
import unicodedata
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
) -> tuple[dict[str, str], dict[str, str | None], dict[str, str]]:
    en_to_code = {e["name"].casefold(): e["code"] for e in sets_data}
    code_to_zh = {e["code"]: e.get("translated_name") for e in sets_data}
    code_to_name = {e["code"]: e["name"] for e in sets_data}
    return en_to_code, code_to_zh, code_to_name


def _build_count_to_codes(sets_data: list[dict]) -> dict[str, list[str]]:
    """Map card_count → [set_codes] for slash-format PLST reconstruction."""
    result: dict[str, list[str]] = {}
    for s in sets_data:
        cc = s.get("card_count")
        if cc:
            result.setdefault(str(cc), []).append(s["code"])
    return result


def _resolve_plst_slash(
    client: httpx.Client,
    collector_number: str,
    name_en: str,
    count_to_codes: dict[str, list[str]],
) -> dict:
    """Resolve a slash-format PLST collector number (e.g. "048/249") to a card.

    Enumerates all sbwsz sets whose card_count equals the slash total, probes
    each compound key PLST/{set_code}-{num}, and accepts the first whose
    English name exactly matches name_en (case-insensitive).
    """
    num_str, total_str = collector_number.split("/", 1)
    num = str(int(num_str))          # strip leading zeros: "048" → "48"

    # Top-level cache for the slash resolution (distinct from per-compound caches)
    path = _cache_path("cards", "PLST_slash", f"{num}_{total_str}")
    cached = _read_cache(path)
    if cached is not None:
        if cached.get("_exhausted"):
            return {}
        return cached

    candidates = count_to_codes.get(total_str, [])
    base_name = _strip_parenthetical(name_en).casefold()

    for code in candidates:
        compound = f"{code}-{num}"
        card = _get_card(client, "PLST", compound)
        if not card:
            continue
        face_name = ((card.get("faces") or [{}])[0]).get("name", "")
        if face_name.casefold() == base_name:
            _write_cache(path, card)
            return card

    _write_cache(path, {"_exhausted": True})
    return {}


def _resolve_set_code(
    set_name_en: str,
    en_to_code: dict[str, str],
    row_ref: str,
    fuzzy_log: list | None = None,
    code_to_name: dict[str, str] | None = None,
) -> str:
    query = set_name_en.casefold()
    if query in en_to_code:
        return en_to_code[query]
    result = process.extractOne(
        query, en_to_code.keys(), scorer=fuzz.WRatio, score_cutoff=80
    )
    if result is not None:
        matched_cf, score, _ = result
        matched_code = en_to_code[matched_cf]
        matched_name_orig = (code_to_name or {}).get(matched_code, matched_cf)
        print(f"[fuzzy] {set_name_en!r} → {matched_name_orig!r} ({score:.0f})", file=sys.stderr)
        if fuzzy_log is not None:
            fuzzy_log.append((set_name_en, matched_name_orig, score))
        return matched_code
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


_TRAILING_PAREN_RE = re.compile(r'\s*\([^)]*\)\s*$')


def _strip_parenthetical(name: str) -> str:
    """Strip ALL trailing parenthetical groups from a card name.

    'Foo (DVD)' → 'Foo'
    'Foo (A) (B)' → 'Foo'
    'Foo (Inner) Bar' → 'Foo (Inner) Bar'  (mid-string parens preserved)
    """
    prev = None
    while prev != name:
        prev = name
        name = _TRAILING_PAREN_RE.sub("", name)
    return name


def _normalize_for_compare(name: str) -> set[str]:
    """Return all acceptable canonical forms of a card name for matching.

    Handles three systematic differences between TCGPlayer and sbwsz:
      - Trailing parentheticals: 'Foo (DVD)' → 'foo'
      - Split cards: 'Find // Finality' also matches sbwsz's 'Find'
      - SLD dash format: 'Miku - Giada, Font of Hope' also matches 'Giada, Font of Hope'
      - Diacritics: 'Arna Kennerud' matches sbwsz's 'Arna Kennerüd'

    A primary lookup is accepted when the row's forms and the card's
    forms share at least one element.
    """
    forms: set[str] = set()

    def add(s: str) -> None:
        s = s.strip()
        if not s:
            return
        while True:
            stripped = _TRAILING_PAREN_RE.sub("", s).strip()
            if stripped == s:
                break
            s = stripped
        if not s:
            return
        forms.add(s.casefold())
        ascii_form = (
            unicodedata.normalize("NFKD", s)
            .encode("ascii", "ignore")
            .decode("ascii")
        )
        if ascii_form and ascii_form.casefold() != s.casefold():
            forms.add(ascii_form.casefold())

    add(name)
    if " // " in name:
        add(name.split(" // ", 1)[0])
    if " - " in name:
        add(name.split(" - ", 1)[1])
    return forms


def _search_card_by_name(
    client: httpx.Client,
    name_en: str,
) -> dict | None:
    """Search sbwsz for any printing with this exact English name to borrow a Chinese name.

    Returns {"name_zh": str, "is_official": bool, "set": str, "collector_number": str} or None.
    Caller MUST leave jihuanshe_price_cny and sbwsz_image_uri null — they belong to a
    different printing and would silently mis-label the row.
    """
    bare_name = _strip_parenthetical(name_en)
    safe = re.sub(r"[^\w\-]", "_", bare_name)
    path = _cache_path("cards", "_name_search", safe)
    cached = _read_cache(path)
    if cached is not None:
        if cached.get("_no_match"):
            return None
        return cached

    search_url = str(httpx.URL(f"{BASE_URL}/result", params={
        "q": f'name:"{bare_name}"',
        "priority_chinese": "true",
        "unique": "oracle_id",
        "view": "0",
        "page_size": "50",
    }))
    try:
        data = _fetch(client, search_url)
    except httpx.HTTPStatusError as exc:
        print(f"[name-search] {name_en!r}: HTTP {exc.response.status_code}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[name-search] {name_en!r}: {exc}", file=sys.stderr)
        return None

    items = data.get("items") if isinstance(data, dict) else []
    base_name = bare_name.casefold()
    for item in (items or []):
        if item.get("name", "").casefold() != base_name:
            continue
        zh_name = item.get("atomic_official_name") or item.get("atomic_translated_name")
        if not zh_name:
            continue
        result = {
            "name_zh": zh_name,
            "is_official": bool(item.get("atomic_official_name")),
            "set": item.get("set", ""),
            "collector_number": item.get("collector_number", ""),
        }
        _write_cache(path, result)
        return result

    _write_cache(path, {"_no_match": True})
    return None


# ── enrichment ────────────────────────────────────────────────────────────────

_SLASH_RE = re.compile(r"^\d+/\d+$")


def _enrich_row(
    idx: int,
    row: dict,
    en_to_code: dict[str, str],
    code_to_zh: dict[str, str | None],
    client: httpx.Client,
    *,
    code_to_name: dict[str, str] | None = None,
    count_to_codes: dict[str, list[str]] | None = None,
    stats: dict | None = None,
    fuzzy_log: list | None = None,
    name_search_log: list | None = None,
) -> dict:
    ref = f"source row {idx} ({row.get('name_en', '?')!r})"
    try:
        set_code = _resolve_set_code(row["set_name_en"], en_to_code, ref, fuzzy_log, code_to_name)
    except ValueError:
        print(f"[no-set] {row['set_name_en']!r} not in sbwsz — enrichment fields will be null", file=sys.stderr)
        if stats is not None:
            stats["no_set"] += 1
            stats["jhs_null"] += 1
        return {**row, "name_zh": None, "set_code": None, "set_name_zh": None,
                "sbwsz_image_uri": None, "jihuanshe_price_cny": None}

    cn = row["collector_number"]
    if set_code == "PLST" and _SLASH_RE.match(cn):
        card = _resolve_plst_slash(client, cn, row["name_en"], count_to_codes or {})
        if card and stats is not None:
            stats["plst_slash_recovered"] += 1
    else:
        card = _get_card(client, set_code, cn)
        if card:
            face_name = ((card.get("faces") or [{}])[0]).get("name", "")
            if face_name:
                if not (_normalize_for_compare(row["name_en"]) & _normalize_for_compare(face_name)):
                    print(
                        f"[wrong-card] {set_code}/{cn}: asked for "
                        f"{row['name_en']!r}, got {face_name!r} — treating as miss",
                        file=sys.stderr,
                    )
                    if stats is not None:
                        stats["primary_lookup_wrong_card"] += 1
                    card = {}

    if not card:
        borrowed = _search_card_by_name(client, row["name_en"])
        if stats is not None:
            if borrowed:
                stats["borrowed_printing"] += 1
            else:
                stats["not_found"] += 1
            stats["jhs_null"] += 1
        if name_search_log is not None and borrowed:
            name_search_log.append({
                "name_en": row["name_en"],
                "borrowed_from": f"{borrowed['set']}/{borrowed['collector_number']}",
            })
        return {
            **row,
            "name_zh": borrowed["name_zh"] if borrowed else None,
            "set_code": set_code,
            "set_name_zh": code_to_zh.get(set_code),
            "sbwsz_image_uri": None,
            "jihuanshe_price_cny": None,
        }

    jhs = _jihuanshe_price(card)
    name_source = (card.get("translation_info") or {}).get("name_source", "")
    has_name = bool(card.get("primary_name"))

    if stats is not None:
        if has_name and name_source == "官方中文":
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


# ── row ID assignment ─────────────────────────────────────────────────────────

def _assign_row_ids(rows: list[dict]) -> list[dict]:
    """Inject a stable row_id ({product_id}_{n}) into each row.

    n is the 0-indexed copy number within rows sharing the same product_id,
    in input order — handles the qty-expansion case where one TCGPlayer line
    becomes multiple physical-card rows.
    """
    counts: dict[int, int] = {}
    result = []
    for row in rows:
        pid = row.get("product_id", 0)
        n = counts.get(pid, 0)
        result.append({**row, "row_id": f"{pid}_{n}"})
        counts[pid] = n + 1
    return result


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich rows.json with sbwsz data.")
    parser.add_argument("input", nargs="?", default="data/rows.json")
    parser.add_argument("-o", "--output", default="data/enriched.json")
    args = parser.parse_args()

    rows: list[dict] = json.loads(Path(args.input).read_text(encoding="utf-8"))
    total = len(rows)

    client = httpx.Client(headers={"User-Agent": USER_AGENT})
    sets_data = _get_sets(client)
    en_to_code, code_to_zh, code_to_name = _build_set_dicts(sets_data)
    count_to_codes = _build_count_to_codes(sets_data)

    stats: dict[str, int] = {
        "official": 0, "community": 0, "null_name": 0, "not_found": 0, "no_set": 0,
        "plst_slash_recovered": 0, "primary_lookup_wrong_card": 0, "borrowed_printing": 0,
        "jhs_populated": 0, "jhs_null": 0,
    }
    fuzzy_log: list[tuple[str, str, float]] = []
    name_search_log: list[dict] = []
    results: list[dict] = []

    for idx, row in enumerate(rows):
        print(f"\r  enriching {idx + 1}/{total} …", end="", flush=True, file=sys.stderr)
        results.append(
            _enrich_row(idx, row, en_to_code, code_to_zh, client,
                        code_to_name=code_to_name, count_to_codes=count_to_codes,
                        stats=stats, fuzzy_log=fuzzy_log, name_search_log=name_search_log)
        )
    print(file=sys.stderr)

    results = _assign_row_ids(results)

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
    print(f"  PLST slash-format recovered:        {stats['plst_slash_recovered']:>4}")
    print(f"  primary lookup wrong card:          {stats['primary_lookup_wrong_card']:>4}")
    print(f"  borrowed from other printing:       {stats['borrowed_printing']:>4}")

    if name_search_log:
        cap = 20
        print(f"\nborrowed-name details ({min(len(name_search_log), cap)} of {len(name_search_log)}):")
        for entry in name_search_log[:cap]:
            print(f"  {entry['name_en']!r} → {entry['borrowed_from']}")

    print(f"\njihuanshe_price_cny coverage:")
    print(f"  populated:  {stats['jhs_populated']:>4}")
    print(f"  null:       {stats['jhs_null']:>4}")

    print(f"\nfuzzy set matches: {len(fuzzy_log)}")
    for inp, match, score in fuzzy_log:
        print(f"  {inp!r} → {match!r} ({score:.0f})")


if __name__ == "__main__":
    main()
