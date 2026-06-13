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

## 2. User workflow (v1 actual)

1. **Scan.** User scans all cards with the TCGPlayer mobile app, sets
   foil/normal during the scan, exports as Apple Numbers or CSV.
2. **Photograph.** One photo per *physical* card using iPhone (HEIC format).
3. **Parse + Enrich.** Run `mtg-parse` then `mtg-enrich` — produces
   `enriched.json` with Chinese names, set names, and Jihuanshe prices.
4. **Review in UI.** `uv run streamlit run src/mtg_xianyu/ui.py` — one card
   per page. The user manually binds a photo to each card (thumbnail grid),
   selects a price (JHS or USD-converted), edits the Chinese name if sbwsz
   returned null, and approves. On approval, the UI writes a JPEG, a JSON
   listing file, and a `.txt` description file to `data/listings/`.
5. **Publish.** For each approved card: copy the description from the UI's
   code block → paste into Xianyu's 宝贝描述 field → upload the JPEG from
   `data/listings/` → set price → publish. The JPEG filename is human-readable
   (`{set_code}-{number} - {name} - {finish_zh}.jpg`) so the file picker is
   navigable.

Photo-to-card matching and price pre-computation are **not separate pipeline
stages** in v1 — they are manual judgment steps in the review UI. See §4.3
and §4.4.

## 3. Data sources

| Source | Authoritative for | Access |
| --- | --- | --- |
| TCGPlayer export | Card identity, finish (Normal/Foil), condition, quantity, USD market price, variant treatment | Local `.numbers` or `.csv` |
| sbwsz.com (大学院废墟) | Chinese card name (official + community translations), Chinese set name, set code, reference image, Jihuanshe CNY price | `https://new.sbwsz.com/api/v1/` |
| User photos | Actual visible condition for the listing | Local folder (`data/mtg_photos/`, HEIC from iPhone) |
| Xianyu | Final publish target | Mobile app, no public API |

**No Scryfall, no MTGJSON, no Wizards Gatherer.** sbwsz subsumes everything we
need from those for this project.

**Anthropic SDK.** The `anthropic` Python SDK is listed as a project dependency
but is **currently unused**. It was originally specified as a vision-model
fallback for photo matching (match.py, which was never built — see §4.3) and
considered for describe.py (which was implemented with templates instead —
see §4.5). Retained in `pyproject.toml` as a foundation for potential future
vision-based features.

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
  `new.sbwsz.com` is an alias that 301-redirects to `mtgch.com`; all runtime
  code uses `mtgch.com` directly to avoid the redirect hop. A docs page exists
  at the old domain but is robots-blocked; discover the schema from live
  responses.
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

## 4. Pipeline stages

### 4.1 Parse — `parse.py`

Read the TCGPlayer export. Normalize columns. **Expand rows where
`Add to Quantity > 1` into one logical row per physical card.** Downstream
stages must not have to think about quantity.

**Row ID scheme.** Each expanded row gets a stable `row_id` of the form
`"{product_id}_{n}"` where `n` is a zero-based index distinguishing multiple
physical copies of the same product (e.g. `276329_0` and `276329_1` for two
copies of Imperial Seal). The `row_id` is stable across re-parses of the same
export — it is derived deterministically, not randomly.

Output `rows.json` — a list of:

```json
{
  "row_id": "276329_0",
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

Validation is fail-fast for fields downstream stages require as non-null keys
(`collector_number`, `set_name_en`, `condition`, `printing`). Fields that
downstream stages can handle as null (`usd_market`, optional descriptive fields)
pass through with a log line.

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
| `image_uris.normal` | `sbwsz_image_uri` | English-print reference image. `image_uris` (not `zhs_image_uris`) because user cards are English prints. |
| `prices.usd` | — | Not stored in `enriched.json`; TCGPlayer's `usd_market` is authoritative for USD |
| `prices.cny` | `jihuanshe_price_cny` | De-facto Jihuanshe / 集换社 market price. Optional — absent, null, or `""` when no trading history for this printing. |

**Jihuanshe prices.** `prices.cny` is the de-facto Jihuanshe / 集换社 market
price for the requested printing. sbwsz names the key generically (`cny`) rather
than `jihuanshe`; it is the number Jihuanshe shows as the market price.

The field is **optional**. Printings without Jihuanshe trading history will have
`prices.cny` absent from the dict, present as JSON null, or present as an empty
string `""`. `_jihuanshe_price(card)` has a strict boundary contract:
**`float | None` out — never a string, never `""`, never `0.0` as an absent
sentinel.**

```python
raw = card.get("prices", {}).get("cny")
if raw is None or raw == "":
    return None
return float(raw)   # "17.27" → 17.27
```

**Enrichment chain for `name_zh` (four tiers, tried in order):**

```
1. /card/{set_code}/{collector_number}/?view=1 — primary lookup
     ↓ (404, name-mismatch verification failure, or no name)
2. PLST slash-format reconstruction (only when set=PLST, collector_number=N/M)
     Enumerate all sbwsz sets with card_count=M; probe PLST/{code}-{N};
     accept first whose faces[0].name matches exactly.
     ↓ (no candidates, or all name-mismatch)
3. /result?q=name:"{name_en}"&priority_chinese=true&unique=oracle_id
     Search for any printing of the card; accept first item whose "name" field
     matches name_en exactly (case-insensitive); take atomic_official_name or
     atomic_translated_name. Leave jihuanshe_price_cny and sbwsz_image_uri null
     — they belong to the matched printing, not the row's printing.
     ↓ (no exact-name match, or no Chinese name in any result)
4. null — surface gap in UI for manual entry.
```

**Name-match verification (primary path, tier 1).** A HTTP 200 response from
`/card/{set}/{number}/` does **not** guarantee the returned card is the one you
asked for — collector_number conflicts between TCGPlayer and sbwsz can return a
real-but-wrong card. `_get_card` verifies `faces[0].name` against the requested
row's `name_en` before accepting the result. Without this step, ~35 rows
silently returned wrong-card data (discovered in v0.4 when a user noticed
Solitude displaying Grief's Chinese name).

`_normalize_for_compare` handles three format quirks:
- Split cards: `"Fire // Ice"` → `"fire ice"`
- SLD dash format: `"Gisela, Blade of Goldnight - Showcase"` → `"gisela blade of goldnight showcase"`
- Diacritics: NFKD-normalised and stripped, so `"Arna Kennerüd"` matches `"Arna Kennerud"`

**Set-name fallback (fuzzy).** TCGPlayer's `Set Name` strings occasionally don't
exactly match sbwsz's English names (e.g. `"Commander: Modern Horizons 3"` vs
sbwsz's form). When the exact `en_to_code` lookup misses, fall back to a fuzzy
match (rapidfuzz WRatio, cutoff 80) against `en_to_code` keys; log every fuzzy
match to stderr for user review. If neither exact nor fuzzy resolves, raise
`ValueError` with the row reference.

**Measured coverage (v0.5).** 803/808 `name_zh` populated (99.4%), with the 5
nulls being structurally unresolvable WPN/Gateway promos not indexed by sbwsz.
704/808 JHS prices populated (87.1%). The 104 missing JHS prices reflect
printings with no Jihuanshe trading history; the UI falls back to the
USD-converted price column for those rows.

**Known limitation — TCGPlayer / sbwsz collector_number mismatches.** Retro
Frame, Showcase, Borderless, and some promo cards use a TCGPlayer-internal
numbering that differs from sbwsz's set numbering. Name-match verification
(above) handles these without special-casing. Known mismatch classes: PLST
promos (handled by tier 2), and certain SLD variant numberings.

**Known limitation — two rows with incorrect set_code.** `MagicFest Cards`
and `SLX Cards` both fuzzy-matched to `Teenage Mutant Ninja Turtles Eternal
Front Cards` at score 86 (above the 80 cutoff). The card lookups 404,
name-search recovers the Chinese names, and user-visible output is correct;
only the internal `set_code` field is wrong. If `set_code` becomes load-bearing
beyond display, raise the fuzzy cutoff to 88+ or add post-fuzzy
card-existence verification.

Output `enriched.json` adds, per row:

```json
{
  "row_id": "276329_0",
  "name_zh": "玉玺",
  "set_code": "2X2",
  "set_name_zh": "双星大师2022",
  "sbwsz_image_uri": "https://images.mtgch.com/sf/normal/front/…/….webp",
  "jihuanshe_price_cny": 1155.91
}
```

`jihuanshe_price_cny` is `null` when `prices.cny` is absent, null, or `""`.
`name_zh` is `null` only when the entire enrichment chain exhausts — **do not
invent.**

### 4.3 Match — `match.py` (stub — not built)

`match.py` exists as an unimplemented stub (`raise NotImplementedError`). The
originally-planned automated matching pipeline (perceptual hash via `imagehash`
+ Anthropic vision-model fallback) was **never built.** During the ui.py Stage 2
design decision (the "Option 3" path), photo-to-card matching was deliberately
absorbed into the UI as a manual thumbnail-click binding step.

Manual binding is appropriate for this use case: the collection is ~800 cards
processed once, not a recurring high-volume pipeline. Manual binding also
handles the long tail of misordered, retaken, and variant-art photos that would
require human review after any automated matcher anyway.

The `imagehash` package remains in `pyproject.toml` but is unused at runtime.

Automated matching is deferred to v2 along with `scan.py` (§4.7).

### 4.4 Price — `price.py` (stub — not built)

`price.py` exists as an unimplemented stub (`raise NotImplementedError`). The
originally-planned separate price-computation stage (producing `priced.json`
with four candidate prices per card) was **never built.** Price selection was
absorbed into `ui.py` as an inline dual-column layout.

**How pricing works in the UI.** Each card's review page shows two price columns
side by side:

- **JHS column**: `jihuanshe_price_cny` from `enriched.json` (greyed out when
  null). A "Use this" button sets `price_cny = jihuanshe_price_cny` with
  `price_source = "jhs"`.
- **USD column**: `usd_market × FX_RATE` (always available). A "Use this"
  button sets `price_cny = round(usd_market × FX_RATE, 2)` with
  `price_source = "usd_converted"`.
- **Manual override**: free-text number input. Sets `price_source = "manual"`.

`FX_RATE = 7.25` is hardcoded in `ui.py` and displayed in the sidebar. Tune
empirically after the first batch of sales.

Sort order in the UI is by `effective_cny = jihuanshe_price_cny or (usd_market
× FX_RATE)`, descending. This is review-order only — it does not affect the
displayed price columns.

The approved listing records `price_cny`, `price_source`, and
`fx_rate_at_approval` (snapshot of the rate used if source is `usd_converted`).

### 4.5 Describe — `describe.py`

Generate a ready-to-paste Xianyu description for each approved card.
**Templated, not LLM-generated** — zero per-card cost, deterministic output,
no API dependency.

**Output format.** A single 4-line string written to `{row_id}.txt`:

```
万智牌 MTG {name_zh}
{name_en_clean}
{set_name_zh}/{set_code} {finish_zh}
{SHOP_POLICY}
```

`name_en_clean` is `name_en` with all trailing parentheticals stripped
(e.g. `"Imperial Seal (Borderless)"` → `"Imperial Seal"`).

`SHOP_POLICY` is a module-level constant, default `"主页满300包邮"`.

**Xianyu has no separate title field.** Line 1 (`万智牌 MTG {name_zh}`) serves
as the implicit listing title.

**`finish_zh` — treatment-aware finish label.** Composed by
`build_finish_zh(name_en, printing)`, which is the shared classifier consumed by
both `describe.py` (description line 3) and `ui.py` (JPEG filename generation).
It strips parentheticals from `name_en` and classifies each suffix into one of
four categories:

| Category | Mapped suffixes | Effect |
|---|---|---|
| Visual treatment | `Borderless` → 异画, `Extended Art` → 扩画, `Retro Frame` → 老框, `Showcase` → 异画 | Prepended before 英文: `异画英文闪` |
| Finish variant | `Foil Etched` → 蚀刻闪, `Rainbow Foil` → 彩虹闪, `Surge Foil` → 潮涌闪 | Replaces the base finish suffix entirely |
| Set code (`^[A-Z0-9]{2,4}$`) | e.g. `DVD`, `IMA`, `A25`, `2X2` | Silently stripped |
| Collector number (`^\d+$`) | e.g. `350`, `280`, `1553` | Silently stripped |
| Unknown | anything else | Stderr warning; base finish used |

Base finish: `Foil` → 闪, `Normal` → 平. Language prefix: always 英文.

Composed examples:

| `name_en` | `printing` | `finish_zh` |
|---|---|---|
| `"Imperial Seal (Borderless)"` | Normal | `异画英文平` |
| `"Esper Sentinel (Retro Frame)"` | Normal | `老框英文平` |
| `"Foo (Foil Etched)"` | Foil | `英文蚀刻闪` |
| `"Foo (Borderless) (Foil Etched)"` | Foil | `异画英文蚀刻闪` |
| `"Jetmir's Garden"` | Foil | `英文闪` |

If a finish variant suffix appears but `printing` is `"Normal"`, a warning is
logged to stderr and the suffix is trusted over the printing field.

**CLI.** `mtg-describe` (registered console script) reads all `data/listings/*.json`,
writes `{row_id}.txt` next to each, and prints a tally of unmapped suffixes at
the end. Re-run after updating the treatment mappings to regenerate all `.txt`
files.

**UI integration.** On approval, `_do_approve` in `ui.py` calls
`build_description(listing)` and writes the `.txt` alongside the `.json` and
`.jpg`. On approved rows, the UI displays the description in an `st.code` block
(with a built-in copy icon) below the `✓ Approved on...` indicator.

### 4.6 Review UI — `ui.py`

Streamlit single-page review interface. All photo binding, price selection, and
approval happens here.

**State machine** (persisted to `data/listings/state.json`):

```
waiting_photo ──[bind photo]──► ready_to_review
              ◄──[unbind]─────
ready_to_review ──[Approve]──► approved  (terminal — see Operations in CLAUDE.md to undo)
                ──[Skip]────► skipped
skipped ──[re-bind + re-price]──► ready_to_review
```

Per-row state persisted in `state.json`:
`{state, photo_path, name_zh_override, price_cny, price_source, approved_at}`

**Layout:**

- **Sidebar**: FX_RATE display, sort radio (Effective CNY / USD market), view
  filter (default / all rows / remaining only / skipped only), progress
  indicator (`X approved · Y skipped · Z remaining`), **📁 Open listings
  folder** button (`subprocess.run(["open", LISTINGS_DIR])`), **↻ Refresh**
  photo pool button.
- **Top row**: Prev / Next navigation buttons with First / Last jump buttons.
  Row position counter. State badge: `✓ APPROVED` (green) or `⊘ SKIPPED`
  (orange); ready and waiting-photo rows are unbadged.
- **Left column**: bound photo at ~400 px height with ✕ Unbind button; OR, when
  no photo is bound, a paginated thumbnail grid (4 columns × 3 rows = 12 per
  page, << First / < Prev / Next > / Last >> buttons) for clicking to bind.
- **Right column**:
  - Card name as `h3`, editable Chinese name field (persisted to
    `state.json`, overrides sbwsz `name_zh`).
  - Two price columns (JHS CNY | USD-converted) each with a "Use this" button
    and the current value displayed.
  - Manual price override number input.
  - Approval hint area: blocking hints in orange (missing price, missing
    Chinese name) or informational hints in blue (which default price will be
    used), or a green "Ready to approve" confirmation.
  - Action buttons (state-aware):
    - **Approve**: primary button; disabled when approval hints block; absent on
      approved rows (replaced by `✓ Approved on {date}` + description code
      block).
    - **Skip**: absent on skipped rows (replaced by `⊘ Already skipped`
      indicator).
    - **Back**: always available; navigates to the previous row.

**Approve action (sequential, safe):**

1. `_build_listing(row, row_entry, FX_RATE, ts)` → listing dict.
2. `_jpg_path_for(listing)` → human-readable JPEG path:
   `{set_code}-{collector_number} - {name_en_safe} - {finish_zh}.jpg`
   where `name_en_safe` strips parentheticals and replaces `/` and `:` with
   `-`. If the path already exists (two copies of the same card), appends
   `(copy {N})`.
3. Update `listing["photo_jpg"]` to the resolved path.
4. `LISTINGS_DIR.mkdir(...)` — ensure directory exists.
5. `Image.open(photo_path).convert("RGB").save(jpg_path, format="JPEG", quality=90)` — HEIC → JPEG.
6. Write `{row_id}.json` (listing dict as JSON).
7. Write `{row_id}.txt` (`build_description(listing)` output).
8. Update `state["rows"][row_id]` in-memory: `state="approved"`, `approved_at=ts`, `price_cny`, `price_source`.
9. `_save_state(state)` — flush to disk.
10. Auto-advance to next `ready_to_review` row; set `_all_caught_up` flag if
    none remain.

**Approval gating.** `_approval_hints(row, row_entry)` is the **single source
of truth** for whether a row is approvable. It collects all blocking conditions
independently (missing Chinese name, unresolvable price) and all informational
hints (which default price will be auto-applied). Both the hint display area and
the Approve button's `disabled` state read from it — no duplicated logic.

**Streamlit footgun.** State mutations (JHS button, USD button, manual price
override, name override) need an explicit `st.rerun()` call immediately after
the mutation, or the UI renders the pre-mutation values for one click before
catching up.

### 4.7 Scan — `scan.py` (v2, future batches only)

Alternative entry point to `parse.py`. Takes a folder of listing photos and
produces the same `rows.json` output, so the downstream pipeline is unchanged.

1. For each photo, OCR the name region to extract a candidate card name.
2. Resolve the name against sbwsz's search endpoint; for cards with multiple
   printings, make a vision-model call comparing the photo against candidates.
3. Foil/normal cannot be reliably inferred from a still photo. Default to
   `Normal` and surface for manual toggling in the review UI.

**Why v2, not v1.** Current ~800-card batch has a TCGPlayer export and
partly-taken photos. Switching workflows mid-collection costs more than it
saves. `scan.py` pays off on the *next* batch.

## 5. Storage formats

### 5.1 `data/listings/state.json`

```json
{
  "rows": {
    "{row_id}": {
      "state": "waiting_photo | ready_to_review | approved | skipped",
      "photo_path": "data/mtg_photos/IMG_5830.HEIC | null",
      "name_zh_override": "str | null",
      "price_cny": "float | null",
      "price_source": "jhs | usd_converted | manual | null",
      "approved_at": "2026-06-06T19:59:20 | null"
    }
  }
}
```

### 5.2 `data/listings/{row_id}.json` (approved listing)

```json
{
  "row_id": "265346_0",
  "product_id": 265346,
  "name_en": "Jetmir's Garden",
  "name_zh": "杰米尔的花园",
  "set_code": "SNC",
  "set_name_en": "Streets of New Capenna",
  "set_name_zh": "新卡佩纳：喧嚣黑街",
  "collector_number": "250",
  "condition": "Near Mint",
  "printing": "Foil",
  "rarity": "Rare",
  "jihuanshe_price_cny": 112.91,
  "usd_market": 17.59,
  "price_cny": 127.53,
  "price_source": "usd_converted",
  "fx_rate_at_approval": 7.25,
  "photo_jpg": "data/listings/SNC-250 - Jetmir's Garden - 英文闪.jpg",
  "photo_heic_source": "data/mtg_photos/IMG_5830.HEIC",
  "approved_at": "2026-06-06T19:59:20"
}
```

### 5.3 `data/listings/{row_id}.txt` (Xianyu description)

```
万智牌 MTG 杰米尔的花园
Jetmir's Garden
新卡佩纳：喧嚣黑街/SNC 英文闪
主页满300包邮
```

## 6. Non-goals (v1)

- **No direct Xianyu scraping for comp prices.** Pricing comes from the
  TCGPlayer USD heuristic and Jihuanshe-via-sbwsz alone. Xianyu actively
  defends against scrapers; account risk isn't worth the marginal signal.
- **No direct Jihuanshe scraping either.** We get Jihuanshe prices via sbwsz
  precisely because sbwsz already does the integration. Hitting Jihuanshe
  directly duplicates work and adds another rate-limited dependency.
- **No automated Xianyu publishing.** Output is paste-ready text + named JPEG.
- **No multi-user or hosted version.** Single-user, local laptop.
- **No card-condition computer vision.** Seller's manually-set condition stands.
- **No non-MTG product lines.** TCGPlayer export's `Product Line` filtered to
  Magic only.
- **No automated photo-to-card matching (v1).** Manual binding in the UI.
  Automated phash / vision matching is deferred to v2 with `scan.py`.

## 7. Roadmap

- **v0** — repo scaffolding, CLAUDE.md, SPEC.md, package skeleton, gitignore.
- **v1** (substantially complete as of v0.5):
  - ✓ `parse.py` — TCGPlayer export → `rows.json`
  - ✓ `enrich.py` — sbwsz lookups with 4-tier fallback, name-match verification;
    803/808 Chinese names, 704/808 JHS prices
  - ✓ `ui.py` — manual photo binding, dual-price selection, approval state
    machine, HEIC→JPEG export (stages 1–6)
  - ✓ `describe.py` — templated 4-line description with treatment-aware
    `finish_zh`; human-readable JPEG filename generation
  - ✓ End-to-end listing artifacts: `.json` + `.jpg` + `.txt` per approved card
- **v2 (deferred)**:
  - `scan.py`: alternative entry point from listing photos, skipping TCGPlayer
    step for future batches
  - `match.py`: automated photo↔card matching via perceptual hash (+ optional
    vision-model fallback for uncertain matches)
  - Playwright-driven Xianyu publish (account risk; TBD)

## 8. Open questions

**Resolved:**

- *"How should low-confidence photo matches be handled?"* — N/A; automated
  matching was deferred. Manual binding in the UI handles all cases, including
  the long tail of retakes and mis-orderings, with zero false positives.
- *"Should the price stage emit one or two suggestions?"* — Resolved: UI
  displays both (JHS column + USD-converted column) with explicit "Use this"
  buttons. Jihuanshe column is greyed out when the price is null; USD column
  always available.
- *"How should describe.py compose the listing text?"* — Resolved: templated
  4-line format (no LLM). Line 1 doubles as the Xianyu listing title.
- *"For cards where sbwsz returns no `name_zh`: null, pinyin, or LLM?"* —
  Resolved: null + manual override field in the UI. Approve button is disabled
  until the user fills in a name or override.
- *"Final enrich coverage?"* — Resolved: 803/808 `name_zh` (99.4%), 5 honest
  nulls (WPN/Gateway promos not indexed by sbwsz). 704/808 JHS prices (87.1%).

**Still open:**

- **FX_RATE calibration.** `FX_RATE = 7.25` is hardcoded in `ui.py`. Tune
  empirically after the first batch of sales; update and re-run `mtg-describe`
  if needed.
- **Treatment suffix mapping is partial.** Common visual treatments and all
  three special finishes are mapped; rarer suffixes (`Anime Borderless`, `Oil
  Slick Raised Foil`, `Future Sight` frame, `White Border`, `JP Alternate Art`,
  etc.) fall to the warn-and-default path. Expand `VISUAL_TREATMENTS` /
  `FINISH_VARIANTS` in `describe.py` as needed when approving cards with
  unmapped treatments.
- **Manual listing workflow scalability.** Per-card Xianyu publish is still
  fully manual (copy description, upload photo, set price). If friction is high
  past ~50 cards, Playwright automation for v2 becomes more attractive.
- **"Un-skip" button omitted in v1.** The manual `state.json` workaround
  (change `"skipped"` to `"ready_to_review"`) is sufficient for now. Revisit
  after real review sessions measure how often skipped rows need revisiting.
- **scan.py OCR / vision engine choice.** Decide when v2 planning starts
  (Tesseract, cloud OCR, or vision-LLM all-in-one).

## 9. The sbwsz MCP server (dev-time only)

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

## 10. Testing

Each built module has a corresponding `tests/test_<module>.py`. Tests use
synthetic in-memory inputs constructed in the test file itself — never read
from `data/`, never make real network calls. HTTP-calling stages use
`httpx.MockTransport` to stub upstream responses with pre-canned (status, body)
tuples. The test suite must pass cleanly (`uv run pytest -v`) before any module
is considered shippable.

Current test files: `test_parse.py`, `test_enrich.py`, `test_ui.py`,
`test_describe.py`. `match.py` and `price.py` have no tests because they have
no implementation.
