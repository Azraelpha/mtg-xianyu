# MTG → Xianyu Listing Tool — Specification

## 1. Problem

Listing Magic: the Gathering single cards on Xianyu (闲鱼 / Goofish, China's
largest second-hand marketplace) is repetitive manual work. For each card the
seller must:

1. Take a photo of the physical card (Xianyu convention — buyers judge
   condition visually before buying).
2. Research current Xianyu listings to set a competitive price.
3. Write a description containing: Chinese card name, English card name,
   condition, foil/normal, set name in both languages, and set/collector
   reference.
4. Publish the listing with the photo attached.

For a collection of ~800 cards this is many hours of work, almost all of it
data entry rather than judgment. This project automates the data entry and
leaves the judgment calls (final price, condition double-check, the actual
act of publishing) to the user.

## 2. User workflow (target)

1. **Scan.** User scans all cards with the TCGPlayer mobile app, sets
   foil/normal during the scan, exports as Apple Numbers or CSV.
2. **Photograph.** One photo per *physical* card. The expanded export defines
   the order.
3. **Enrich.** User runs the pipeline: parse → enrich → match → price →
   describe.
4. **Review.** A local Streamlit UI shows one card per page with the photo,
   all enriched fields, two suggested price columns, and the generated
   description. User confirms or edits each row.
5. **Publish.** Approved rows export to `listings.json`. v1: user pastes into
   the Xianyu mobile app manually.

## 3. Data sources

| Source | Authoritative for | Access |
| --- | --- | --- |
| TCGPlayer export | Card identity, finish (Normal/Foil), condition, quantity, USD market price, variant treatment | Local `.numbers` or `.csv` |
| sbwsz.com (大学院废墟) | Chinese card name (official + community translations), Chinese set name, set code, reference image, Jihuanshe CNY price | `https://new.sbwsz.com/api/v1/` |
| User photos | Actual visible condition for the listing | Local folder (`data/mtg_photos/`, HEIC from iPhone) |
| Xianyu | Final publish target | Mobile app, no public API |

**No Scryfall, no MTGJSON, no Wizards Gatherer.** sbwsz subsumes everything we
need from those for this project.

### 3.1 The Bloomburrow cliff

Wizards of the Coast stopped printing paper Simplified Chinese cards after
Bloomburrow (released August 2, 2024). Sets released afterward — Duskmourn,
Foundations, Aetherdrift, Tarkir: Dragonstorm, and everything since — have
**no official Chinese name** for any card. Other data sources (Scryfall,
MTGJSON, Gatherer) have nothing for these. sbwsz has them, using community
translations seeded from 旅法师营地 plus their own corrections, marking
unofficial translations on the UI in blue.

This is *the* reason sbwsz beats every other Chinese MTG data source for this
use case. For the current ~800-card batch, almost everything is pre-Bloomburrow
(top set is Modern Horizons 3, released June 2024 — just before the cliff),
so for v1 the post-Bloomburrow path is a minor edge case. For future batches
it will be the norm.

### 3.2 sbwsz operational notes

- Public HTTP API at `https://mtgch.com/api/v1/` (canonical base).
  `new.sbwsz.com` is an alias serving the same API; use `mtgch.com` in all
  runtime code to avoid the 301 redirect hop. A docs page exists at the old
  domain but is robots-blocked; discover the schema from live responses.
- **sbwsz broadly discourages automated access.** `robots.txt` covers the full
  site. Always identify the tool via `User-Agent`; query specific cards only;
  never enumerate sets or search results. The API is a community courtesy.
- Community-maintained by a small team. Treat as a soft dependency: plan for
  occasional downtime; cache aggressively so the pipeline can re-run offline
  against cached data.
- Rate limit ourselves to ≥100 ms between requests. Send a descriptive
  `User-Agent` identifying the tool and contact info.
- **Query parameters materially change the response shape.** The same path
  with different query strings returns structurally different JSON (e.g.
  `prices.cny` and the `versions` array are absent on the bare card endpoint
  but present with `?view=1`). Always record the **full URL including query
  string** as the cache key. Never assume two URLs with the same path return
  equivalent data.

## 4. Pipeline

Six stages in v1, starting from a TCGPlayer export. A v2 alternative entry
point (`scan.py`, §4.7) will identify cards directly from listing photos for
future batches. Both entry points produce the same `rows.json` shape;
everything downstream is unchanged.

### 4.1 Parse — `parse.py`

Read the TCGPlayer export. Normalize columns. **Expand rows where
`Add to Quantity > 1` into one logical row per physical card.** Downstream
stages must not have to think about quantity.

Output `rows.json` — a list of:

```json
{
  "product_id": 276329,
  "set_name_en": "Double Masters 2022",
  "name_en": "Imperial Seal (Borderless)",
  "collector_number": "354",
  "rarity": "Mythic",
  "condition": "Near Mint",
  "printing": "Normal",
  "usd_market": 167.87,
  "tcg_photo_url": "https://tcgplayer-cdn.tcgplayer.com/product/276329_in_200x200.jpg"
}
```

### 4.2 Enrich — `enrich.py`

**API base URL.** `https://mtgch.com/api/v1/` is the canonical base.
`new.sbwsz.com` is an alias that 301-redirects to `mtgch.com`; all runtime
code uses `mtgch.com` directly to avoid the redirect hop.

**Bootstrap.** `GET /api/v1/sets/` returns a JSON array of set objects. From it
build two in-memory dicts:

```
en_to_code  : { entry["name"]: entry["code"]            for entry in sets_data }
code_to_zh  : { entry["code"]: entry["translated_name"] for entry in sets_data }
```

Relevant fields per set entry:

| API field | Type | Notes |
|---|---|---|
| `code` | str | Uppercase set code, e.g. `"2X2"` |
| `name` | str | English set name used as lookup key |
| `translated_name` | str \| null | Simplified Chinese name; null for sets with no Chinese edition |

Persist the full response to `data/cache/sbwsz/sets.json` wrapped in
`{"fetched_at": <unix_ts>, "data": [...]}`. Refresh if the timestamp is older
than 7 days.

**Per row.** `GET /api/v1/card/{set_code}/{collector_number}/?view=1`. The
`?view=1` parameter is **required** — the bare endpoint omits `prices.cny` and
the `versions` array entirely. Persist the response to
`data/cache/sbwsz/cards/{set_code}/{number}.json` keyed on the full URL
including query string (no TTL — card data is immutable).

Relevant fields per card entry and how they map to our canonical shape:

| API field | Our field | Precedence / notes |
|---|---|---|
| `atomic_official_name` | `name_zh` (primary) | Wizards-blessed official Simplified Chinese name |
| `atomic_translated_name` | `name_zh` (fallback) | Community translation; used only when `atomic_official_name` is null |
| — | `name_zh = null` | Valid output if both are null (post-Bloomburrow, no translation yet). **Never invent.** |
| `set_translated_name` | `set_name_zh` | Chinese name for the set as returned per-card; may differ slightly from `code_to_zh` for the same code |
| `image_uris.normal` | `sbwsz_image_uri` | English-print reference image. We use `image_uris` (not `zhs_image_uris`) because user cards are English prints; `match.py`'s phash comparison needs the reference to match the physical card being photographed |
| `prices.usd` | — | Not stored in `enriched.json`; TCGPlayer's `usd_market` is authoritative for USD |
| `prices.usd_foil` | — | Same — not stored |
| `prices.cny` | `jihuanshe_price_cny` | De-facto Jihuanshe / 集换社 market price; sbwsz uses the generic key `cny`. Optional — absent, null, or `""` when no trading history for this printing. |

**Jihuanshe prices.** `prices.cny` is the de-facto Jihuanshe / 集换社 market
price for the requested printing. sbwsz names the key generically (`cny`) rather
than `jihuanshe`; it is the number Jihuanshe shows as the market price.

The field is **optional**. Printings without Jihuanshe trading history will have
`prices.cny` absent from the dict, present as JSON null, or present as an empty
string `""`. `_jihuanshe_price(card)` has a strict boundary contract:
**`float | None` out — never a string, never `""`, never `0.0` as an absent
sentinel.** This mirrors `_collector_number`'s contract in `parse.py`: raw API
types go in, clean Python types come out.

```python
raw = card.get("prices", {}).get("cny")
if raw is None or raw == "":
    return None
return float(raw)   # "17.27" → 17.27
```

Output `enriched.json` adds, per row:

```json
{
  "name_zh": "真伪莫辨",
  "set_code": "SLD",
  "set_name_zh": "秘室珍品",
  "sbwsz_image_uri": "https://images.mtgch.com/sf/normal/front/…/….webp",
  "jihuanshe_price_cny": 17.27
}
```

`jihuanshe_price_cny` is `null` when `prices.cny` is absent, null, or `""` —
`price.py` handles this gracefully (Jihuanshe column greyed out in UI, USD
column remains pickable).

`name_zh` is `null` when both `atomic_official_name` and
`atomic_translated_name` are absent — **do not invent.** The UI surfaces these
gaps for manual entry.

**Fuzzy fallback.** TCGPlayer's `Set Name` strings occasionally don't exactly
match sbwsz's English names (e.g. `"Commander: Modern Horizons 3"` vs sbwsz's
form). When the exact `en_to_code` lookup misses, fall back to a fuzzy match
(rapidfuzz WRatio, cutoff 80) against `en_to_code` keys; log every fuzzy match
to stderr for user review. If neither exact nor fuzzy resolves, raise
`ValueError` with the row reference — the failure is unrecoverable downstream.

### 4.3 Match — `match.py`

Closed-world matching. Given N photos and M enriched rows, produce
photo_path → row assignments.

**HEIC handling.** User photos arrive in Apple's HEIC format. At module
startup, register `pillow-heif` as a Pillow opener so subsequent
`PIL.Image.open()` calls handle HEIC transparently:

```python
from pillow_heif import register_heif_opener
register_heif_opener()
```

For the vision-model fallback below, decode the HEIC and re-encode as JPEG
in memory before the API call — Anthropic's vision endpoint accepts
JPEG/PNG/WebP/GIF, not HEIC.

1. Compute perceptual hash (`imagehash.phash`) of each user photo and each
   row's `sbwsz_image_uri`. Cache reference hashes alongside cards.
2. For each user photo, the candidate set is *all rows* — not "the next row
   in sequence." Robust to skipped photos, retakes, mis-orderings.
3. Take the row with smallest Hamming distance below `threshold_low`.
4. For matches in `[threshold_low, threshold_high]`, fall back to a
   vision-model call: "Is the card in this photo the same as the card in this
   reference image? Yes / No / Unsure."
5. Above `threshold_high`, mark the photo unmatched.

Output `matched.json` adds `{photo_path}` per row; `unmatched.json` captures
photos that couldn't be confidently assigned.

### 4.4 Price — `price.py`

Two independent suggestions per card, both written to the output so the UI
can show both and the user can pick.

**Source A — USD heuristic** (always available, since every row has a
`usd_market` from TCGPlayer):

```
fast_sell_usd  =  usd_market × fx_rate × 0.55
hold_out_usd   =  usd_market × fx_rate × 1.00
```

`fx_rate` defaults to 7.2 (configurable). The 0.55 / 1.00 multipliers are
tunable defaults intended to be calibrated empirically after the first
batch sells. All multipliers and `fx_rate` are configurable in a top-level
project config (location TBD).

**Source B — Jihuanshe** (when `jihuanshe_price_cny` is present):

```
fast_sell_jhs  =  jihuanshe_price_cny × 0.60
hold_out_jhs   =  jihuanshe_price_cny × 0.90
```

The discount applied vs. Jihuanshe reflects Xianyu's nature: lower trust,
less infrastructure, more competition. Xianyu sellers typically undercut
Jihuanshe; Jihuanshe is the more "authoritative" Chinese reference price.

**Note on data availability.** sbwsz's Jihuanshe feed is intermittent; some
enrichment runs will produce mostly-null `jihuanshe_price_cny`. `price.py`
and the UI must degrade to USD-only gracefully when this field is null.

**Why both, why let the user pick.** The two sources can disagree
significantly. The USD-derived figure reflects what global collectors think
the card is worth; the Jihuanshe figure reflects what Chinese collectors
actually pay. For mainstream cards the two should track; for cards with
strong Chinese-specific demand (e.g. Portal Three Kingdoms cards, certain
flavor-themed reprints) Jihuanshe can be substantially higher. For cards
with weak Chinese demand the inverse. Showing both anchors and letting the
user choose is more informative than picking one and hiding the other.

Output `priced.json` adds four candidate prices per card:

```json
{
  "fast_sell_usd": 663.85,
  "hold_out_usd":  1207.00,
  "fast_sell_jhs": 725.04,
  "hold_out_jhs":  1087.56
}
```

Any Jihuanshe field is `null` if `jihuanshe_price_cny` was `null`. The UI
must handle that case gracefully (greyed-out Jihuanshe column, USD column
remains pickable).

### 4.5 Describe — `describe.py`

Generate a Xianyu listing title and description body. **Templated, not
LLM-generated**, for predictability and zero per-card cost.

- **Title** (≤30 Chinese chars, Xianyu's practical limit): Chinese name + key
  variant + finish marker, e.g. `御用密令 异画 普通 双重大师2022`.
- **Description body**: bilingual block listing condition, finish, set
  (Chinese + English + code), collector number, English name. Closes with a
  sbwsz data attribution line. Example (SLD/1995, Fact or Fiction, NM, Normal):

  ```
  中文名：真伪莫辨 / Fact or Fiction
  系列：秘室珍品 / Secret Lair Drop (SLD) · #1995
  品相：近况完好 · Near Mint
  版本：普通 · Normal
  数据参考：mtgch.com (大学院废墟)
  ```

### 4.6 Review UI — `ui.py`

Streamlit. One card per page. Layout:

- **Left**: user photo and sbwsz reference image side by side.
- **Right**: every field editable. Pricing as a 2×2 grid (USD vs Jihuanshe,
  fast vs hold) with the four suggestions as radio buttons, plus a free-text
  override input. The chosen value goes into the final listing. Generated
  description in a textarea.
- **Buttons**: `Approve`, `Skip`, `Flag for follow-up`.

Approved rows append to `listings.json`. The state file supports resuming
across sessions — closing the tab and reopening must not lose progress.

**JPEG export on approval.** When a row is approved, write a JPEG copy of
the photo next to the HEIC original (`IMG_0123.heic` → `IMG_0123.jpg`) and
record the JPEG path in the listing entry. The HEIC remains the master file;
the JPEG is the portable artifact for any upload path that isn't iOS-to-iOS
— desktop browser uploads, future Playwright automation, vision-API requests
during re-runs. Quality 90, sRGB, no metadata. The conversion is idempotent;
re-approving a row overwrites the JPEG.

### 4.7 Scan — `scan.py` (v2, future batches only)

Alternative entry point to `parse.py`. Takes a folder of listing photos and
produces the same `rows.json` output, so the downstream pipeline is
unchanged.

1. For each photo, OCR the name region to extract a candidate card name.
2. Resolve the name against sbwsz's `search_cards` endpoint with the
   Chinese or English name; for cards with multiple printings, make a
   vision-model call comparing the photo against candidates.
3. Foil/normal cannot be reliably inferred from a still photo. Default to
   `Normal` and surface for manual toggling in the review UI.

**Why v2, not v1.** Current ~800-card batch has a TCGPlayer export and
partly-taken photos. Switching workflows mid-collection costs more than it
saves. `scan.py` pays off on the *next* batch.

**Tradeoff vs. live-scanning apps.** Live scanners (TCGPlayer, ManaBox)
disambiguate variant printings *while the user is holding the card* — flip
it, check set symbol, tilt for foil shine. `scan.py` disambiguates from the
photo alone, after the card has been filed away. Expect a tail of 10–20% of
cards needing manual lookup; acceptable cost for the workflow simplification.

## 5. Non-goals (v1)

- **No direct Xianyu scraping for comp prices.** Pricing comes from the
  TCGPlayer USD heuristic and Jihuanshe-via-sbwsz alone. Xianyu actively
  defends against scrapers; account risk isn't worth the marginal signal.
- **No direct Jihuanshe scraping either.** We get Jihuanshe prices via sbwsz
  precisely because sbwsz already does the integration. Hitting Jihuanshe
  directly duplicates work and adds another rate-limited dependency.
- **No automated Xianyu publishing.** Output is paste-ready text + photo
  paths.
- **No multi-user or hosted version.** Single-user, local laptop.
- **No card-condition computer vision.** Seller's manually-set condition
  stands.
- **No non-MTG product lines.** TCGPlayer export's `Product Line` filtered
  to Magic only.

## 6. Roadmap

- **v0 (now)** — repo scaffolding, CLAUDE.md, SPEC.md, package skeleton,
  gitignore.
- **v1** — pipeline end-to-end on the current TCGPlayer export. Manual
  publish.
- **v2 (planned)** — `scan.py`: alternative entry point that builds
  `rows.json` from listing photos for future batches. See §4.7.
- **v3 (maybe never)** — Playwright-driven Xianyu publish behind explicit
  opt-in. Only if/when the account-risk profile is acceptable.

A previously-considered v3 (Xianyu comp-price scraping) is removed — we
already get Jihuanshe prices via sbwsz, and Jihuanshe is upstream of Xianyu
pricing in practice. Xianyu scraping would add account risk without adding
useful signal.

## 7. Open questions

- Calibration of the USD and Jihuanshe price multipliers — to be tuned
  empirically by observing which suggestion the user most often accepts
  during the first dozen sales.
- For cards where sbwsz returns no `name_zh` (post-Bloomburrow without
  community translation): accept null, fall back to pinyin, or attempt an
  LLM translation? Leaning null + UI prompt for v1.
- For `scan.py` (§4.7): OCR engine choice (Tesseract / cloud OCR /
  vision-LLM all-in-one). Decide when v2 starts.
- Jihuanshe coverage on the v0.2 enrich run: 723 of 808 rows populated
  (89.5%). The remaining ~10% degrade to USD-only in the review UI. This
  is the empirical justification for keeping the dual-source design rather
  than collapsing to USD-only.
- Are the 55 cards with 404 responses recoverable via a fourth-tier
  PLST-style fallback (lookup by name across other printings)?
  Implement and measure.

## 8. The sbwsz MCP server (dev-time only)

There's a community MCP server, `lieyanqzu/sbwsz-mcp` (npm:
`sbwsz-mcp-server`), that wraps the sbwsz API as MCP tools:
`get_card_by_set_and_number`, `search_cards`, `get_sets`, `get_set`,
`get_set_cards`. Installable in Claude Code's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "sbwsz": {
      "command": "npx",
      "args": ["sbwsz-mcp-server"]
    }
  }
}
```

Useful for **dev-time interactive lookups** when working on the project with
Claude Code — "look up 御用密令 and tell me which variants exist," etc. It is
**not** used at pipeline runtime; `enrich.py` calls the HTTP API directly
via `httpx`, no Node or MCP runtime needed.

## 9. Testing

Each stage has a corresponding `tests/test_<stage>.py`. Tests use synthetic
in-memory inputs constructed in the test file itself — never read from `data/`,
never make real network calls. HTTP-calling stages use `httpx.MockTransport` to
stub upstream responses with pre-canned (status, body) tuples. The test suite
must pass cleanly (`uv run pytest -v`) before any stage is considered shippable.