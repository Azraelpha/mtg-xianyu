"""Public enrichment module with safety fixes layered over the v0.5 implementation.

The previous implementation is preserved in ``_enrich_impl.py`` so this review
branch stays easy to audit/revert.  This module tightens three behaviours:
- mutable sbwsz card/JHS price responses expire after 24 hours;
- fuzzy set matching requires a stronger score (88 instead of 80);
- PLST slash-resolution caches use the same freshness policy.
"""

import hashlib
import re
import sys
import time

import httpx
from rapidfuzz import fuzz, process

from mtg_xianyu import _enrich_impl as _impl

# Re-export the implementation surface (including private helpers used by tests).
for _name in dir(_impl):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_impl, _name)

CARD_CACHE_TTL = 24 * 3600
FUZZY_SET_SCORE_CUTOFF = 88
_last_request_at = 0.0

# Keep monkeypatching enrich._cache_path effective for helpers defined in _impl.
_original_cache_path = _impl._cache_path
_cache_path = _original_cache_path


def _cache_path_proxy(kind: str, *parts: str):
    return globals()["_cache_path"](kind, *parts)


_impl._cache_path = _cache_path_proxy


def _fetch(client: httpx.Client, url: str, *, _retries: int = 3) -> dict | list:
    """Rate-limited HTTP fetch using this public module's resettable clock."""
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if _last_request_at > 0 and elapsed < RATE_DELAY:
        time.sleep(RATE_DELAY - elapsed)
    _last_request_at = time.monotonic()
    response = client.get(url)
    if response.status_code == 429 and _retries > 0:
        wait = float(response.headers.get("Retry-After", 10))
        print(f"\n[429] rate limited; waiting {wait:.0f}s …", file=sys.stderr)
        time.sleep(wait)
        return _fetch(client, url, _retries=_retries - 1)
    response.raise_for_status()
    return response.json()


_impl._fetch = _fetch


def _read_fresh_card_cache(path):
    cached = _read_cache(path)
    if not isinstance(cached, dict):
        return None
    if cached.get("_cache_kind") != "card_response_v1":
        # Legacy caches contain mutable prices with no timestamp. Refresh once.
        return None
    if time.time() - cached.get("fetched_at", 0) >= CARD_CACHE_TTL:
        return None
    return cached.get("data")


def _write_card_cache(path, data: dict) -> None:
    _write_cache(path, {
        "_cache_kind": "card_response_v1",
        "fetched_at": time.time(),
        "data": data,
    })


def _url_cache_path(kind: str, url: str, *display_parts: str):
    """Return a readable cache path whose identity includes the full URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    leaf = f"{display_parts[-1]}__{digest}" if display_parts else digest
    parents = display_parts[:-1]
    return _cache_path(kind, *parents, leaf)


def _get_card(client: httpx.Client, set_code: str, number: str) -> dict:
    """Fetch one printing, refreshing mutable JHS price data every 24 hours."""
    url = f"{BASE_URL}/card/{set_code}/{number}/?view=1"
    path = _url_cache_path("cards", url, set_code, number)
    cached = _read_fresh_card_cache(path)
    if cached is not None:
        return cached
    try:
        data = _fetch(client, url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            print(
                f"[404] {set_code}/{number} not in sbwsz — enrichment fields will be null",
                file=sys.stderr,
            )
            return {}
        raise
    _write_card_cache(path, data)
    return data


_impl._get_card = _get_card


def _search_card_by_name(
    client: httpx.Client,
    name_en: str,
) -> dict | None:
    """Borrow a Chinese name using a full-URL, expiring cache key."""
    bare_name = _strip_parenthetical(name_en)
    search_url = str(httpx.URL(f"{BASE_URL}/result", params={
        "q": f'name:"{bare_name}"',
        "priority_chinese": "true",
        "unique": "oracle_id",
        "view": "0",
        "page_size": "50",
    }))
    safe_name = re.sub(r"[^\w\-]", "_", bare_name) or "unnamed"
    path = _url_cache_path("cards", search_url, "_name_search", safe_name)
    cached = _read_fresh_card_cache(path)
    if cached is not None:
        return None if cached.get("_no_match") else cached

    try:
        data = _fetch(client, search_url)
    except httpx.HTTPStatusError as exc:
        print(
            f"[name-search] {name_en!r}: HTTP {exc.response.status_code}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(f"[name-search] {name_en!r}: {exc}", file=sys.stderr)
        return None

    items = data.get("items") if isinstance(data, dict) else []
    base_name = bare_name.casefold()
    for item in items or []:
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
        _write_card_cache(path, result)
        return result

    _write_card_cache(path, {"_no_match": True})
    return None


_impl._search_card_by_name = _search_card_by_name


def _resolve_plst_slash(
    client: httpx.Client,
    collector_number: str,
    name_en: str,
    count_to_codes: dict[str, list[str]],
) -> dict:
    """Resolve slash-format PLST numbers without pinning stale JHS prices forever."""
    num_str, total_str = collector_number.split("/", 1)
    num = str(int(num_str))
    resolution_url = (
        f"{BASE_URL}/internal/plst-resolution/{num}/{total_str}"
        f"?name={name_en}"
    )
    path = _url_cache_path(
        "cards", resolution_url, "PLST_slash", f"{num}_{total_str}"
    )
    cached = _read_fresh_card_cache(path)
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
            _write_card_cache(path, card)
            return card

    exhausted = {"_exhausted": True}
    _write_card_cache(path, exhausted)
    return {}


_impl._resolve_plst_slash = _resolve_plst_slash


def _resolve_set_code(
    set_name_en: str,
    en_to_code: dict[str, str],
    row_ref: str,
    fuzzy_log: list | None = None,
    code_to_name: dict[str, str] | None = None,
) -> str:
    """Resolve a set name, rejecting the known score-86 false-positive class."""
    query = set_name_en.casefold()
    if query in en_to_code:
        return en_to_code[query]
    result = process.extractOne(
        query,
        en_to_code.keys(),
        scorer=fuzz.WRatio,
        score_cutoff=FUZZY_SET_SCORE_CUTOFF,
    )
    if result is not None:
        matched_cf, score, _ = result
        matched_code = en_to_code[matched_cf]
        matched_name_orig = (code_to_name or {}).get(matched_code, matched_cf)
        print(
            f"[fuzzy] {set_name_en!r} → {matched_name_orig!r} ({score:.0f})",
            file=sys.stderr,
        )
        if fuzzy_log is not None:
            fuzzy_log.append((set_name_en, matched_name_orig, score))
        return matched_code
    raise ValueError(f"no set_code for {set_name_en!r} in {row_ref}")


_impl._resolve_set_code = _resolve_set_code

# Public aliases must point at the patched functions, not the originals copied above.
globals().update({
    "_fetch": _fetch,
    "_get_card": _get_card,
    "_search_card_by_name": _search_card_by_name,
    "_resolve_plst_slash": _resolve_plst_slash,
    "_resolve_set_code": _resolve_set_code,
})

main = _impl.main

if __name__ == "__main__":
    main()
