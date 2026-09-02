# MTG → Xianyu Listing Tool

A pipeline that turns a TCGPlayer collection export into ready-to-publish Xianyu
(闲鱼 / Goofish) listings for Magic: the Gathering cards — with Chinese card
name, Chinese set name, condition, foil status, and suggested CNY price.

See `@./SPEC.md` for the full design.

## Status

v0.5 — v1 UI complete (stages 1–6), describe.py landed with treatment-aware
finish_zh, real Xianyu listings producible end-to-end. parse.py and enrich.py
are production-ready (803/808 enriched, 5 honest nulls). match.py and price.py
remain as unimplemented stubs; their functionality was deliberately absorbed into
ui.py (manual photo binding + inline dual-price selection — see Pipeline below).
Currently refining UI based on real listing workflow feedback. Automated Xianyu
publish (playwright) deferred to v2.

## Stack

- Python 3.11+
- `numbers-parser` — read TCGPlayer Apple Numbers export (CSV also accepted)
- `httpx` — sbwsz HTTP API client
- `Pillow` + `pillow-heif` — image I/O; HEIC decode (iPhone-native photos)
- `imagehash` — installed but currently unused; was planned for match.py perceptual
  hashing, which was never built; manual photo binding in the UI replaced it
- Anthropic SDK — available for future vision-based features; currently unused
  (describe.py uses template-based generation, not LLM)
- `streamlit` — local review/edit/approve UI (active, core to workflow)
- `rapidfuzz` — fuzzy matching used in enrich.py name normalization
- (deferred) `playwright` — only if/when we attempt Xianyu browser automation

## External data — one source, no fallbacks

- **sbwsz.com / 大学院废墟** is the only Chinese MTG data source the runtime
  pipeline touches. It provides Chinese card names (official printings plus
  community translations for post-Bloomburrow sets), Chinese set names, set
  codes, reference card images, and Jihuanshe (集换社) CNY market prices.
  Public HTTP API at `https://new.sbwsz.com/api/v1/`. Community-maintained.
- **TCGPlayer export** is the authoritative local source for: card identity
  (`TCGplayer Id`, set, collector number, name with variant), finish, condition,
  quantity, USD market price.

There is **no Scryfall, no MTGJSON, no Wizards Gatherer** call in this pipeline.
sbwsz subsumes everything we'd need from those.

## Dev-time MCP

The community `sbwsz-mcp-server` (npm: `sbwsz-mcp-server`, GitHub:
`lieyanqzu/sbwsz-mcp`) is installable in Claude Code via `claude mcp add` or
by adding an `mcpServers` block to `~/.claude.json` (user-level) or
`.claude/settings.json` (project-level) for interactive sbwsz lookups while
developing. It is **not** used at pipeline runtime — runtime calls the HTTP API
directly from Python. See SPEC §8.

## Project layout

```
.
├── CLAUDE.md
├── SPEC.md
├── pyproject.toml
├── src/mtg_xianyu/
│   ├── parse.py       # TCGPlayer export → normalized rows (expands multi-qty)
│   ├── enrich.py      # enrichment pipeline: expiring cache + safe set match
│   ├── match.py       # stub — NOT BUILT; manual photo binding in ui.py instead
│   ├── price.py       # stub — NOT BUILT; dual-price logic lives in ui.py instead
│   ├── storage.py     # shared atomic UTF-8 text/JSON persistence
│   ├── describe.py    # treatment-aware finish_zh + 4-line Xianyu description
│   └── ui.py          # Streamlit workflow (photo bind, dual-price selection,
│                      #   HEIC→JPEG export, approval gates, state machine)
├── data/
│   ├── mtg_photos/    # user photos in HEIC (gitignored)
│   ├── cache/
│   │   └── sbwsz/
│   │       ├── cards/     # URL-keyed responses with 24-hour TTL (gitignored)
│   │       └── sets.json  # set list with fetched_at timestamp (gitignored)
│   ├── rows.json          # parse.py output (gitignored)
│   ├── enriched.json      # enrich.py output (gitignored)
│   └── listings/          # approve output: per-row .json + .jpg + .txt
│       └── state.json     # UI state machine (gitignored)
└── tests/
    ├── test_parse.py
    ├── test_enrich.py
    ├── test_ui.py
    └── test_describe.py
```

## Pipeline

```
collection.csv ──► parse.py ──► rows.json
                                    │
                                    ▼
                   enrich.py ──► enriched.json   (calls sbwsz)
                                    │
                                    ▼
                   ui.py  (Streamlit — manual workflow)
                     • sort / browse by price
                     • bind HEIC photo to each row
                     • view dual prices (JHS CNY + USD-converted)
                     • set price_cny / price_source
                     • edit name_zh override when sbwsz returned null
                     • approve → writes {set_code}-{num} - {name} - {finish_zh}.jpg
                                        + {row_id}.json
                                        + {row_id}.txt  (describe.py output)
```

**match.py and price.py are stubs and will remain so.** Their originally planned
functionality (perceptual-hash photo matching, separate priced.json stage) was
deliberately collapsed into ui.py's manual workflow during the Stage 2 design
decision (Option 3). This is not a TODO — it's an architectural choice. The
manual binding produces better results for a ~800-card one-time job than an
automated matcher that would need manual review anyway.

`mtg-describe` can also be run standalone as a CLI to regenerate all `.txt` files
from existing `.json` listings (e.g., after updating the treatment mappings).

## Always

- Expand `Add to Quantity > 1` rows into one logical row per physical card in
  `parse.py`. **Downstream code must never see multi-quantity rows.**
- Preserve variant treatment in card names — `Imperial Seal (Borderless)` must
  stay `Imperial Seal (Borderless)` through every stage; `finish_zh` in the
  description encodes the treatment, and `_safe_name_en` strips it from the
  JPEG filename to avoid duplication.
- Set a descriptive `User-Agent` on every sbwsz request, identifying the tool
  and a contact. Space requests ≥100 ms apart. sbwsz is community-run; be
  respectful.
- Cache every sbwsz response under `data/cache/sbwsz/`, keyed by the full
  request URL including query parameters. Card responses contain mutable price
  data, carry a timestamp, and expire after 24 hours. Cache card 404s with an
  explicit negative sentinel under the same TTL so misses are not re-requested
  on every run.
- Treat the **TCGPlayer export** as authoritative for: card identity, finish
  (Normal/Foil), condition, USD market price, quantity.
- Treat **sbwsz** as authoritative for: Chinese card name, Chinese set name,
  set code, Jihuanshe CNY market price (when present), reference image URI.
- Produce **both** price suggestions for every card in the UI (USD-derived and
  Jihuanshe-derived). Display both and let the user pick. Never show only one.
- Every module that gets built lands with a corresponding test file under
  `tests/test_<module>.py` in the same commit. Never commit without tests.
- Parse-stage validation is fail-fast: quantity and product ID must be positive
  integers; product ID, card name, collector number, set name, condition, and
  finish must be present; finish must be Normal or Foil and is never defaulted
  from a blank value. Required export columns are checked before normalization.
  Failures raise ValueError with a column or row reference. Fields downstream
  can handle as null pass through with a log line.
- Enrich-stage disk input is revalidated before network access. Malformed cache
  entries degrade to cache misses; malformed live sbwsz response shapes fail
  with the endpoint in the error and are never cached.
- Cache keys are derived from the full request URL including query string. Two
  URLs with the same path but different query parameters are different cache
  entries. Human-readable cache path components are bounded and sanitized;
  never allow API or export values to introduce path separators or `..`.
- Stages must remain re-runnable from disk state. Each stage reads its input
  and writes its output; no in-memory state passes between stages.
- Persistent JSON and text outputs use the shared atomic writers in
  `storage.py`; never write directly to a final pipeline, cache, description,
  or UI-state path.
- After Claude Code completes a multi-part task, verify each item against the
  original prompt before approving. Coding agents are lossy on tail items.
- State mutations in Streamlit (button clicks, `on_change` callbacks that write
  `state.json`) need an explicit `st.rerun()` after, or the UI shows
  one-click-behind state.
- Validate `enriched.json` and `state.json` before rendering. Migrate missing
  legacy state fields atomically, but never replace structurally invalid state;
  stale row IDs are preserved and ignored by current progress/photo-pool logic.
- `_approval_hints` in `ui.py` is the single source of truth for "is this row
  approvable." Both the visual hint area and the Approve button's `disabled`
  state read from it. Never duplicate that logic elsewhere.
- `build_finish_zh` in `describe.py` is the single source of truth for
  treatment classification (Borderless → 异画, Foil Etched → 蚀刻闪, etc.).
  Both `describe.py` (description line 3) and `ui.py` (JPEG filename) call it.
  Never duplicate.

## Never

- Never commit the actual collection export or user photos — gitignored.
- Never call Scryfall, MTGJSON, or any other MTG data service from runtime
  code. If you think you need one, the answer is sbwsz or "ask first."
- Never write to TCGPlayer, Jihuanshe, or Xianyu without an explicit
  `--publish` flag. Default mode is read/research only.
- Never invent a Chinese name. If sbwsz returns no `name_zh`, leave the field
  `null` and surface the gap in the UI for manual entry.
- Never collapse different finishes (Normal vs Foil) into one row.
- Never display only one price suggestion in the UI when both are available.
  Showing the user both anchors and letting them choose is a design property,
  not an implementation detail.

## Gotchas learned during design

- **Simplified Chinese paper printings stopped after Bloomburrow (Aug 2024).**
  Any post-Bloomburrow set has no official Chinese name; sbwsz provides
  community translations for these. The current ~800-card batch is mostly
  pre-Bloomburrow, so for v1 this is a minor case.
- **Foil and condition are already in the TCGPlayer export.** Don't build a UI
  to re-collect them; build a UI to *verify* them.
- **TCG Market Price is USD; Jihuanshe is CNY.** Don't mix units. The UI shows
  both and lets the user choose; the approved listing records which source won.
- **Photo matching is manual in the UI, not automated.** The original plan was
  perceptual hashing in match.py; this was abandoned in favour of a manual
  binding grid in ui.py. Each user photo is a thumbnail the user drags/clicks
  to bind to a row. Closed-world: every photo maps to exactly one row.
- **sbwsz is community-run and `robots.txt`-protected.** Don't crawl; query
  specific cards. Cache. Honor the implicit rate limit.
- **User photos are HEIC** (Apple's iPhone-native format). Register
  `pillow-heif` as a Pillow opener at the start of any module that opens a
  photo. The approve action converts HEIC → JPEG in memory via
  `Image.open(...).convert("RGB").save(...)`. Do not assume `.jpg` files exist
  on disk by default.
- **sbwsz's `/api/v1/card/` endpoint returns a lean response by default;
  only `?view=1` includes `prices.cny` and the `versions` array. Always
  include `?view=1` on card requests.**
- **Uniform failure patterns almost always indicate a normalization mismatch,
  not missing data.** If a batch of cards all fail for the same structural
  reason (e.g. every unrecoverable card shares trailing parenthetical suffixes),
  diagnose the pattern before accepting the gap as structural. The 18-card gap
  in enrich v0.3 looked like missing sbwsz data but was entirely a
  name-normalization mismatch.
- **A HTTP 200 response from sbwsz's `/card/{set}/{number}/` does NOT guarantee
  the returned card is the one you asked for** — collector_number conflicts
  between TCGPlayer and sbwsz can return a real-but-wrong card. `_get_card`
  verifies `faces[0].name` against the requested row's `name_en` before
  accepting. Same principle as the PLST slash collision verification.
- **Xianyu's seller form has no separate title field** — the first line of the
  description is treated as the title implicitly. `describe.py` produces a
  single 4-line description string; line 1 (`万智牌 MTG {name_zh}`) doubles as
  the listing title.
- **Card name parenthetical suffixes encode four different things:**
  - Visual treatments (`Borderless`, `Retro Frame`, `Extended Art`, `Showcase`)
    — combine with base finish: `异画英文闪`, `老框英文平`, etc.
  - Finish variants (`Foil Etched`, `Rainbow Foil`, `Surge Foil`) — replace the
    base finish suffix entirely: `英文蚀刻闪`, `英文彩虹闪`, `英文潮涌闪`.
  - Set codes (`DVD`, `IMA`, `A25`, `2X2`) — silently stripped.
  - Collector numbers (`350`, `280`, `1553`) — silently stripped.
  - `describe.py`'s `_classify_and_compose_finish` handles all four categories
    with warn-and-continue fallback for unmapped treatments.
- **JPEG filenames at approve time use human-readable format**
  `{set_code}-{collector_number} - {name_en_safe} - {finish_zh}.jpg`
  so the macOS file picker can identify cards by name when uploading to Xianyu.
  `name_en_safe` strips parentheticals (treatment is already in `finish_zh`) and
  replaces `/` and `:` with `-`. Collision with an existing file appends
  `(copy {N})`. Listings approved before this naming change keep their old
  `{row_id}.jpg` names on disk.

## Commands

```bash
# Run from project root

uv run mtg-parse   data/collection.csv      # → data/rows.json
uv run mtg-enrich  data/rows.json           # → data/enriched.json

# Review UI — main workflow
uv run streamlit run src/mtg_xianyu/ui.py

# Regenerate .txt description files from all existing .json listings
# (re-run after updating treatment mappings in describe.py)
uv run mtg-describe
```

## Operations

### Re-approving a row

Approval is terminal in v1 — the UI has no re-approve button. To redo
an approved row manually, its editing and photo-binding controls remain
read-only until the state and existing artifacts are reset together:

1. Find and delete the three files written at approve time:
   - `data/listings/{row_id}.json`
   - `data/listings/{row_id}.txt`
   - The JPEG (human-readable name like `SNC-250 - Jetmir's Garden - 英文闪.jpg`;
     check `photo_jpg` in the JSON before deleting)
2. Edit `data/listings/state.json`: find the row entry and change
   `"state": "approved"` back to `"ready_to_review"`, clear
   `"approved_at"` to `null`. Leave `photo_path`, `name_zh_override`,
   and `price_cny`/`price_source` in place (they'll re-populate the
   UI fields when the row re-opens).
3. Restart the UI (or it will pick up the state change on the next
   page render).

### Photo directory changes between sessions

If photos are added, moved, or deleted between UI sessions, click
**↻ Refresh** in the sidebar photo pool section to rescan. The pool is
built once per session on startup; it does not auto-refresh. Nested photo
folders are supported; the UI shows relative paths and keys thumbnails by their
complete normalized paths so duplicate filenames do not collide.

### Xianyu listing workflow (per card)

1. Approve card in UI → the `.json`, `.jpg`, and `.txt` files land in
   `data/listings/`.
2. Copy the 4-line description from the `st.code` block shown below the
   approval timestamp (it has a built-in copy icon).
3. Switch to the Xianyu seller form:
   - Paste into **宝贝描述**.
   - Set **成色** dropdown to match the condition.
   - Upload the JPEG via file picker: click **📁 Open listings folder** in
     the UI sidebar to open `data/listings/` in Finder first — filenames
     are human-readable, so the right photo is easy to find.
   - Enter the CNY price.
   - Publish.

## Open decisions

- Calibration of USD and Jihuanshe price multipliers — tune empirically after
  selling the first batch. (`FX_RATE = 7.25` is hardcoded in `ui.py`.)
- Whether v2 attempts Playwright-driven Xianyu publish (account risk, session
  handling complexity).
- Treatment suffix mapping is partial — the six most common visual treatments
  and three finish variants are mapped; rarer suffixes (`Anime Borderless`,
  `Oil Slick Raised Foil`, `Future Sight` frame, `White Border`, `JP Alternate
  Art`, etc.) fall to the warn-and-default path. Expand `VISUAL_TREATMENTS` /
  `FINISH_VARIANTS` in `describe.py` as cards with unmapped treatments are
  approved.
