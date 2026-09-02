"""Enrich parsed rows with Chinese metadata from sbwsz.com (大学院废墟).

Bootstraps the sbwsz set list once (cached weekly under
data/cache/sbwsz/sets.json), resolves each row's set code via exact then
fuzzy match, and calls get_card_by_set_and_number per card (responses cached
under readable, full-URL-hashed paths in data/cache/sbwsz/cards/). Adds name_zh,
set_code, set_name_zh, sbwsz_image_uri, and jihuanshe_price_cny to each row.
Missing Chinese names are left null — never invented. Writes enriched.json.
"""

import argparse
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
from pathlib import Path

import httpx
from rapidfuzz import fuzz, process

from mtg_xianyu.storage import atomic_write_json

BASE_URL = "https://mtgch.com/api/v1"
USER_AGENT = "mtg-xianyu/0.1 (https://github.com/Azraelpha/mtg-xianyu)"
SETS_TTL = 7 * 24 * 3600   # seconds before the set list is re-fetched
CARD_CACHE_TTL = 24 * 3600  # card prices and negative lookups expire daily
FUZZY_SET_SCORE_CUTOFF = 88
RATE_DELAY = 0.5            # minimum seconds between live requests
_CACHE_COMPONENT_MAX = 96
_URL_CACHE_READABLE_MAX = 64
_UNSAFE_CACHE_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_NOT_FOUND_CACHE_KEY = "_not_found"

_last_request_at: float = 0.0

_REQUIRED_ROW_STRING_FIELDS = (
    "name_en",
    "set_name_en",
    "collector_number",
    "condition",
    "printing",
)


# ── cache helpers ─────────────────────────────────────────────────────────────

def _safe_cache_component(
    value: str, *, max_length: int = _CACHE_COMPONENT_MAX
) -> str:
    """Return one bounded, relative filename component for cache readability."""
    component = _UNSAFE_CACHE_CHARS_RE.sub("_", str(value)).strip("._")
    return (component or "item")[:max_length]


def _cache_path(kind: str, *parts: str) -> Path:
    base = Path("data/cache/sbwsz")
    safe_kind = _safe_cache_component(kind)
    if parts:
        safe_parts = [_safe_cache_component(part) for part in parts]
        return base / safe_kind / Path(*safe_parts[:-1]) / f"{safe_parts[-1]}.json"
    return base / f"{safe_kind}.json"


def _read_cache(path: Path) -> object | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _write_cache(path: Path, data: dict | list) -> None:
    atomic_write_json(path, data)


def _is_fresh_timestamp(value: object, ttl: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value):
        return False
    age = time.time() - value
    return 0 <= age < ttl


def _read_fresh_card_cache(path: Path) -> dict | None:
    """Return an unexpired card-cache payload, ignoring legacy raw entries."""
    cached = _read_cache(path)
    if not isinstance(cached, dict):
        return None
    if cached.get("_cache_kind") != "card_response_v1":
        return None
    if not _is_fresh_timestamp(cached.get("fetched_at"), CARD_CACHE_TTL):
        return None
    data = cached.get("data")
    return data if isinstance(data, dict) else None


def _write_card_cache(path: Path, data: dict) -> None:
    _write_cache(path, {
        "_cache_kind": "card_response_v1",
        "fetched_at": time.time(),
        "data": data,
    })


def _url_cache_path(kind: str, url: str, *display_parts: str) -> Path:
    """Build a readable cache path whose identity includes the complete URL."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    if not display_parts:
        return _cache_path(kind, digest)
    readable = _safe_cache_component(
        display_parts[-1], max_length=_URL_CACHE_READABLE_MAX
    )
    leaf = f"{readable}__{digest}"
    return _cache_path(kind, *display_parts[:-1], leaf)


# ── input and response validation ─────────────────────────────────────────────

def _type_name(value: object) -> str:
    return type(value).__name__


def _load_rows(path: Path) -> list[dict]:
    """Load and validate the canonical parse-stage output."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load enrichment input {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(
            f"invalid enrichment input {path}: expected a JSON array, "
            f"got {_type_name(data)}"
        )

    for idx, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(
                f"invalid enrichment input row {idx}: expected an object, "
                f"got {_type_name(row)}"
            )
        ref = f"input row {idx} ({row.get('name_en', '?')!r})"
        product_id = row.get("product_id")
        if (
            isinstance(product_id, bool)
            or not isinstance(product_id, int)
            or product_id < 1
        ):
            raise ValueError(
                f"'product_id' must be a positive integer in {ref}: "
                f"{product_id!r}"
            )
        for field in _REQUIRED_ROW_STRING_FIELDS:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{field!r} must be a non-empty string in {ref}: {value!r}"
                )
        if row["printing"] not in {"Normal", "Foil"}:
            raise ValueError(
                f"'printing' must be 'Normal' or 'Foil' in {ref}: "
                f"{row['printing']!r}"
            )
        usd_market = row.get("usd_market")
        if usd_market is not None and (
            isinstance(usd_market, bool)
            or not isinstance(usd_market, (int, float))
            or not math.isfinite(usd_market)
            or usd_market < 0
        ):
            raise ValueError(
                f"'usd_market' must be null or a finite non-negative number "
                f"in {ref}: {usd_market!r}"
            )
    return data


def _validate_sets_response(data: object, source: str) -> list[dict]:
    if not isinstance(data, list):
        raise ValueError(
            f"invalid sbwsz sets response from {source}: expected a JSON array, "
            f"got {_type_name(data)}"
        )
    for idx, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(
                f"invalid sbwsz sets response from {source}: entry {idx} "
                f"must be an object, got {_type_name(entry)}"
            )
        for field in ("code", "name"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"invalid sbwsz sets response from {source}: entry {idx} "
                    f"has invalid {field!r}: {value!r}"
                )
        translated_name = entry.get("translated_name")
        if translated_name is not None and not isinstance(translated_name, str):
            raise ValueError(
                f"invalid sbwsz sets response from {source}: entry {idx} has "
                f"invalid 'translated_name': {translated_name!r}"
            )
    return data


def _validate_card_response(data: object, source: str) -> dict:
    if not isinstance(data, dict):
        raise ValueError(
            f"invalid sbwsz card response from {source}: expected a JSON object, "
            f"got {_type_name(data)}"
        )
    faces = data.get("faces")
    if not isinstance(faces, list) or not faces or not all(
        isinstance(face, dict) for face in faces
    ):
        raise ValueError(
            f"invalid sbwsz card response from {source}: 'faces' must be a "
            "non-empty array of objects"
        )
    primary_name = data.get("primary_name")
    if primary_name is not None and not isinstance(primary_name, str):
        raise ValueError(
            f"invalid sbwsz card response from {source}: 'primary_name' must "
            f"be a string or null, got {_type_name(primary_name)}"
        )
    for field in ("prices", "translation_info"):
        value = data.get(field)
        if value is not None and not isinstance(value, dict):
            raise ValueError(
                f"invalid sbwsz card response from {source}: {field!r} must "
                f"be an object or null, got {_type_name(value)}"
            )
    prices = data.get("prices") or {}
    raw_cny = prices.get("cny")
    if raw_cny not in (None, ""):
        if isinstance(raw_cny, bool):
            raise ValueError(
                f"invalid sbwsz card response from {source}: 'prices.cny' "
                f"must be numeric or null, got {raw_cny!r}"
            )
        try:
            cny = float(raw_cny)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid sbwsz card response from {source}: 'prices.cny' "
                f"must be numeric or null, got {raw_cny!r}"
            ) from exc
        if not math.isfinite(cny) or cny < 0:
            raise ValueError(
                f"invalid sbwsz card response from {source}: 'prices.cny' "
                f"must be finite and non-negative, got {raw_cny!r}"
            )
    translation_info = data.get("translation_info") or {}
    name_source = translation_info.get("name_source")
    if name_source is not None and not isinstance(name_source, str):
        raise ValueError(
            f"invalid sbwsz card response from {source}: "
            f"'translation_info.name_source' must be a string or null"
        )
    for idx, face in enumerate(faces):
        name = face.get("name")
        if name is not None and not isinstance(name, str):
            raise ValueError(
                f"invalid sbwsz card response from {source}: face {idx} has "
                f"invalid 'name': {name!r}"
            )
        for field in ("image_uris", "zhs_image_uris"):
            value = face.get(field)
            if value is not None and not isinstance(value, dict):
                raise ValueError(
                    f"invalid sbwsz card response from {source}: face {idx} "
                    f"field {field!r} must be an object or null"
                )
            normal = (value or {}).get("normal")
            if normal is not None and not isinstance(normal, str):
                raise ValueError(
                    f"invalid sbwsz card response from {source}: face {idx} "
                    f"field {field!r}.normal must be a string or null"
                )
    return data


def _validate_search_response(data: object, source: str) -> list[dict]:
    if not isinstance(data, dict):
        raise ValueError(
            f"invalid sbwsz search response from {source}: expected a JSON "
            f"object, got {_type_name(data)}"
        )
    items = data.get("items")
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError(
            f"invalid sbwsz search response from {source}: 'items' must be an "
            "array of objects"
        )
    for idx, item in enumerate(items):
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"invalid sbwsz search response from {source}: item {idx} has "
                f"invalid 'name': {name!r}"
            )
        for field in (
            "atomic_official_name",
            "atomic_translated_name",
            "set",
            "collector_number",
        ):
            value = item.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(
                    f"invalid sbwsz search response from {source}: item {idx} "
                    f"has invalid {field!r}: {value!r}"
                )
    return items


def _is_borrowed_name_result(data: dict) -> bool:
    return (
        isinstance(data.get("name_zh"), str)
        and bool(data["name_zh"])
        and isinstance(data.get("is_official"), bool)
        and isinstance(data.get("set"), str)
        and isinstance(data.get("collector_number"), str)
    )


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _fetch(client: httpx.Client, url: str, *, _retries: int = 3) -> object:
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
    try:
        return r.json()
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON from sbwsz endpoint {url}: {exc}") from exc


def _get_sets(client: httpx.Client) -> list[dict]:
    path = _cache_path("sets")
    cached = _read_cache(path)
    if isinstance(cached, dict) and _is_fresh_timestamp(
        cached.get("fetched_at"), SETS_TTL
    ):
        try:
            return _validate_sets_response(cached.get("data"), f"cache {path}")
        except ValueError:
            pass

    url = f"{BASE_URL}/sets/"
    data = _validate_sets_response(_fetch(client, url), url)
    _write_cache(path, {"fetched_at": time.time(), "data": data})
    return data


def _get_card(client: httpx.Client, set_code: str, number: str) -> dict:
    # Always ?view=1 — bare endpoint omits prices.cny and versions array.
    # Cache key corresponds to the full URL (path + query string).
    url = f"{BASE_URL}/card/{set_code}/{number}/?view=1"
    path = _url_cache_path("cards", url, set_code, number)
    cached = _read_fresh_card_cache(path)
    if cached is not None:
        if cached.get(_NOT_FOUND_CACHE_KEY) is True:
            return {}
        try:
            return _validate_card_response(cached, f"cache {path}")
        except ValueError:
            pass
    try:
        data = _validate_card_response(_fetch(client, url), url)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            print(f"[404] {set_code}/{number} not in sbwsz — enrichment fields will be null", file=sys.stderr)
            _write_card_cache(path, {_NOT_FOUND_CACHE_KEY: True})
            return {}
        raise
    _write_card_cache(path, data)
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
    resolution_url = (
        f"{BASE_URL}/internal/plst-resolution/{num}/{total_str}?name={name_en}"
    )
    path = _url_cache_path(
        "cards", resolution_url, "PLST_slash", f"{num}_{total_str}"
    )
    cached = _read_fresh_card_cache(path)
    if cached is not None:
        if cached.get("_exhausted") is True:
            return {}
        try:
            return _validate_card_response(cached, f"cache {path}")
        except ValueError:
            pass

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

    _write_card_cache(path, {"_exhausted": True})
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
        query,
        en_to_code.keys(),
        scorer=fuzz.WRatio,
        score_cutoff=FUZZY_SET_SCORE_CUTOFF,
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
    search_url = str(httpx.URL(f"{BASE_URL}/result", params={
        "q": f'name:"{bare_name}"',
        "priority_chinese": "true",
        "unique": "oracle_id",
        "view": "0",
        "page_size": "50",
    }))
    safe_name = re.sub(r"[^\w\-]", "_", bare_name) or "unnamed"
    path = _url_cache_path(
        "cards", search_url, "_name_search", safe_name
    )
    cached = _read_fresh_card_cache(path)
    if cached is not None:
        if cached.get("_no_match") is True:
            return None
        if _is_borrowed_name_result(cached):
            return cached

    try:
        data = _fetch(client, search_url)
    except httpx.HTTPStatusError as exc:
        print(f"[name-search] {name_en!r}: HTTP {exc.response.status_code}", file=sys.stderr)
        return None
    except httpx.RequestError as exc:
        print(f"[name-search] {name_en!r}: {exc}", file=sys.stderr)
        return None

    items = _validate_search_response(data, search_url)
    base_name = bare_name.casefold()
    for item in items:
        item_name = item.get("name")
        if not isinstance(item_name, str) or item_name.casefold() != base_name:
            continue
        zh_name = item.get("atomic_official_name") or item.get("atomic_translated_name")
        if not isinstance(zh_name, str) or not zh_name:
            continue
        result = {
            "name_zh": zh_name,
            "is_official": bool(item.get("atomic_official_name")),
            "set": item.get("set") or "",
            "collector_number": item.get("collector_number") or "",
        }
        _write_card_cache(path, result)
        return result

    _write_card_cache(path, {"_no_match": True})
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
            verified = bool(face_name) and bool(
                _normalize_for_compare(row["name_en"])
                & _normalize_for_compare(face_name)
            )
            if not verified:
                detail = f"got {face_name!r}" if face_name else "response had no face name"
                print(
                    f"[wrong-card] {set_code}/{cn}: asked for "
                    f"{row['name_en']!r}, {detail} — treating as miss",
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

    borrowed = None
    if not card.get("primary_name"):
        borrowed = _search_card_by_name(client, row["name_en"])
        if borrowed:
            if stats is not None:
                stats["borrowed_printing"] += 1
            if name_search_log is not None:
                name_search_log.append({
                    "name_en": row["name_en"],
                    "borrowed_from": f"{borrowed['set']}/{borrowed['collector_number']}",
                })

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
    image_uri = (face.get("image_uris") or face.get("zhs_image_uris") or {}).get("normal")

    return {
        **row,
        "name_zh": _name_zh(card) or (borrowed["name_zh"] if borrowed else None),
        "set_code": set_code,
        "set_name_zh": code_to_zh.get(set_code),
        "sbwsz_image_uri": image_uri,
        "jihuanshe_price_cny": jhs,
    }


# ── row ID assignment ─────────────────────────────────────────────────────────

_ROW_IDENTITY_FIELDS = (
    "name_en",
    "set_name_en",
    "collector_number",
    "printing",
    "condition",
)


def _row_identity_key(row: dict) -> tuple[str, ...]:
    """Return stable fields that distinguish variants sharing a product ID."""
    return tuple(str(row.get(field) or "").strip() for field in _ROW_IDENTITY_FIELDS)


def _assign_row_ids(rows: list[dict]) -> list[dict]:
    """Inject a stable row_id ({product_id}_{n}) into each row.

    Rows sharing a product_id are ranked by authoritative identity fields so
    heterogeneous variants retain their suffixes if the export is reordered.
    Exact duplicate copies are interchangeable and keep their input order.
    The returned list itself remains in the original input order.
    """
    indices_by_product: dict[int, list[int]] = {}
    for idx, row in enumerate(rows):
        indices_by_product.setdefault(row.get("product_id", 0), []).append(idx)

    copy_number_by_index: dict[int, int] = {}
    for indices in indices_by_product.values():
        ranked = sorted(indices, key=lambda idx: (_row_identity_key(rows[idx]), idx))
        for copy_number, idx in enumerate(ranked):
            copy_number_by_index[idx] = copy_number

    return [
        {
            **row,
            "row_id": f"{row.get('product_id', 0)}_{copy_number_by_index[idx]}",
        }
        for idx, row in enumerate(rows)
    ]


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich rows.json with sbwsz data.")
    parser.add_argument("input", nargs="?", default="data/rows.json")
    parser.add_argument("-o", "--output", default="data/enriched.json")
    args = parser.parse_args()

    rows = _load_rows(Path(args.input))
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
    atomic_write_json(out, results)
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
