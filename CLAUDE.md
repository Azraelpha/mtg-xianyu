# MTG → Xianyu Listing Tool

A pipeline that turns a TCGPlayer collection export into ready-to-publish Xianyu
(闲鱼 / Goofish) listings for Magic: the Gathering cards — with Chinese card
name, Chinese set name, condition, foil status, and suggested CNY price.

See `@./SPEC.md` for the full design.

## Status

v0.1 — parse.py complete (12 tests passing); enrich.py next.

## Stack

- Python 3.11+
- `numbers-parser` — read TCGPlayer Apple Numbers export (CSV also accepted)
- `httpx` — sbwsz HTTP API client
- `Pillow` + `imagehash` — perceptual hashing for photo ↔ stock-image matching
- `pillow-heif` — HEIC decode support (user photos are iPhone-native HEIC)
- Anthropic SDK (vision) — fallback for low-confidence photo matches only
- `streamlit` — local review/edit UI
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
│   ├── enrich.py      # sbwsz lookups: Chinese names, set names, image, prices
│   ├── match.py       # photo files ↔ rows (phash first, vision fallback)
│   ├── price.py       # dual-source price suggestions (USD heuristic + Jihuanshe)
│   ├── describe.py    # Chinese listing title + bilingual description body
│   └── ui.py          # Streamlit review/edit interface
├── data/
│   ├── mtg_photos/    # user photos in HEIC (gitignored)
│   ├── cache/
│   │   └── sbwsz/
│   │       ├── cards/     # per-card responses, keyed by set/number (gitignored)
│   │       └── sets.json  # set list with fetched_at timestamp (gitignored)
│   └── (collection file also gitignored)
└── tests/
```

## Pipeline

```
collection.numbers ─► parse.py    ─► rows.json
                                          │
                                          ▼
                      enrich.py    ─► enriched.json   (calls sbwsz)
                                          │
photos/*.jpg ────────► match.py    ─► matched.json
                                          │
                                          ▼
                      price.py     ─► priced.json
                                          │
                                          ▼
                      describe.py  ─► listings.json
                                          │
                                          ▼
                      ui.py (review / edit / approve / export)
```

Each stage reads from disk and writes to disk; stages are independently
re-runnable.

## Always

- Expand `Add to Quantity > 1` rows into one logical row per physical card in
  `parse.py`. **Downstream code must never see multi-quantity rows.**
- Preserve variant treatment in card names — `Imperial Seal (Borderless)` must
  stay `Imperial Seal (Borderless)` through every stage; the Chinese title
  should carry the treatment where possible.
- Set a descriptive `User-Agent` on every sbwsz request, identifying the tool
  and a contact. Space requests ≥100 ms apart. sbwsz is community-run; be
  respectful.
- Cache every sbwsz response under `data/cache/sbwsz/`, keyed by request URL.
  Card data is effectively immutable; price data carries a timestamp.
- Treat the **TCGPlayer export** as authoritative for: card identity, finish
  (Normal/Foil), condition, USD market price, quantity.
- Treat **sbwsz** as authoritative for: Chinese card name, Chinese set name,
  set code, Jihuanshe CNY market price (when present), reference image URI.
- Produce **both** price suggestions for every card in `price.py` (USD-derived
  and Jihuanshe-derived). The UI displays both and lets the user pick.
- Every pipeline stage lands with a corresponding test file under
  `tests/test_<stage>.py` in the same commit as the implementation.
  Never commit a stage without its tests.
- Parse-stage validation is fail-fast: any field that downstream
  stages need as a non-null key (collector number, set name,
  condition, finish) must raise ValueError with a row reference.
  Fields that downstream stages can handle as null (price, optional
  descriptive fields) pass through with a log line.
- Cache keys are derived from the full request URL including query
  string. Two URLs with the same path but different query parameters
  are different cache entries.
- Stages must remain re-runnable from disk state. Each stage reads
  its input file and writes its output file; no in-memory state passes
  between stages.
- After Claude Code completes a multi-part task, verify each item
  against the original prompt before approving. Coding agents are
  lossy on tail items.

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
- **TCG Market Price is USD; Jihuanshe is CNY.** Don't mix units. The price
  stage emits both; the UI shows both.
- **Photo matching is closed-world.** Every user photo corresponds to exactly
  one row in the expanded export. sbwsz's per-card response includes a
  reference image; perceptual hash against that handles >80% with zero LLM cost.
- **sbwsz is community-run and `robots.txt`-protected.** Don't crawl; query
  specific cards. Cache. Honor the implicit rate limit.
- **User photos are HEIC** (Apple's iPhone-native format). Register
  `pillow-heif` as a Pillow opener at the start of any module that opens a
  photo. Vision-API calls and final listing exports must convert HEIC → JPEG
  in memory; do not assume `.jpg` files exist on disk by default.
- **sbwsz's `/api/v1/card/` endpoint returns a lean response by default;
  only `?view=1` includes `prices.cny` and the `versions` array. Always
  include `?view=1` on card requests.**
- **Uniform failure patterns almost always indicate a normalization
  mismatch, not missing data.** If a batch of cards all fail for the same
  structural reason (e.g. every unrecoverable card shares trailing
  parenthetical suffixes), diagnose the pattern before accepting the gap
  as structural. The 18-card gap in enrich v0.3 looked like missing sbwsz
  data but was entirely a name-normalization mismatch.

## Commands

_To be filled in as scripts land. Tentative:_

```bash
uv run mtg-parse   data/collection.csv       # → data/rows.json
uv run mtg-enrich  data/rows.json            # → data/enriched.json
uv run mtg-match   data/enriched.json data/mtg_photos/   # → data/matched.json
uv run mtg-price   data/matched.json         # → data/priced.json
uv run mtg-describe data/priced.json         # → data/listings.json

# Review UI — must be run from project root (data/ paths are relative)
uv run streamlit run src/mtg_xianyu/ui.py
```

## Open decisions

- Calibration of USD and Jihuanshe price multipliers (tune empirically after
  selling the first batch).
- Whether v3 attempts Playwright-driven Xianyu publish (account risk).