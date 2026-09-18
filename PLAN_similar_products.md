# Plan: product detail popup with similar products and cross-dispensary price comparison

Status: **phases 0–3 done on the dev site** (2026-09-17): `build_similarity.py` writes the tables, `/api/products/{id}/detail` serves them, the popup is live on :8001 with per-offer checkboxes. Remaining: phase 4 (nightly hook, run on prod). Embeddings, index and neighbour matrices for the dev DB are in `data/` (`embeddings_dev.npy`, `products_dev.faiss`, `nn_dev_I.npy`/`nn_dev_D.npy`); nothing wired into the app yet.

## Goal

Clicking a card opens a popup with:

1. **The product** — image, name, brand, type/subcategory, strain, weight, THC, price, dispensary, link to the dispensary's page.
2. **Same product at other dispensaries** — the identical SKU (brand + product + size) everywhere it's stocked, with each price, the market average/median/min, and a clear read on the price you're looking at ("$4 above the average of 12 stores", "cheapest of 7").
3. **Similar products** — semantically close alternatives (same brand's other flavours, comparable cartridges at nearby prices), ranked by embedding similarity.

Item 2 is the one that needs to be *right*; item 3 only needs to be *useful*.

## What the data already gives us (audited 2026-09-17)

| | Dutchie (52.7k rows) | Jane / Vireo (34.4k rows) |
| --- | --- | --- |
| Catalog-level product id | `libraryProductId` — **53.5%** of rows; 4,110 ids, 2,717 at 2+ dispensaries, one at 85 | `product_id` — **100%**; 7,242 ids, 4,136 at 2+ stores (**91% of rows** share an id), one at 49 |
| Brand id | `brandId` 94% | `product_brand_id` 99% |
| Description | none in the CSV | 100% |
| Exact brand+name across stores | 8,045 of 35,266 names at 2+ dispensaries | — |

Consequences:

- Within Jane, "same product elsewhere" is an exact join on `product_id`. No model needed.
- Within Dutchie, `libraryProductId` links half the rows exactly; the rest need matching.
- Across platforms (a Wana gummy at a Dutchie store vs a LivWell) there is no shared key; matching is the only way.
- Embedding text can't lean on descriptions for Dutchie; it's name + brand + structured fields.

## Architecture

**All the ML runs offline, nightly, after the import. The web app stays a SQLite reader.**

```
import(s) → products table
             │
             ▼
build_similarity.py   (nightly, ~5 min on the Mac)
   1. text per product  →  MiniLM embeddings (384-d, CPU)
   2. FAISS index       →  top-K neighbours per product         → similar_products table
   3. product groups    →  exact keys + matched pairs → union-find → product_groups table
                            + per-group price stats               → group_prices view
             │
             ▼
app.py: GET /api/products/{id}/detail  → one SQL round-trip each for product / group / similar
static/index.html: card click → modal
```

FAISS is used to *build* the neighbour table and the candidate pairs; the request path never loads it. The index file is still written to disk (`data/products.faiss` + id map) so a live semantic search box can be added later without re-embedding.

### 1. Embeddings

- Model: `sentence-transformers/all-MiniLM-L6-v2` (384-d, ~80 MB, CPU-only is fine). Alternative if quality disappoints: `BAAI/bge-small-en-v1.5`. Both run 87k short strings in 2–4 min on the Intel Mac.
- Text template (one line per product):
  `"{brand} {name} | {product_type} {product_subcategory} | {strain_type} | {weight_label} | {thc_display}"` + `" | {description[:200]}"` when present.
  Brand first and repeated in the name is fine — it should dominate.
- Store as float32 in `data/embeddings.npy` aligned to `products.id`; rebuilt from scratch nightly (cheap enough; avoids stale rows).

### 2. Similar products (FAISS)

- `IndexFlatIP` over L2-normalised vectors = exact cosine. 87k × 384 × 4 B ≈ 134 MB; search for all rows takes seconds. No need for HNSW/IVF at this size.
- For each product, take the top **30** by cosine, drop rows from the *same group* (those are the "same product" list), keep **top 12** across any dispensary.
- Table: `similar_products(product_row INTEGER, similar_row INTEGER, score REAL, rank INTEGER)` — ~1M rows, indexed on `product_row`.
- Optional later: a CLIP image index for visual similarity (packaging), since Dutchie has images but no descriptions.

### 3. Same product = product groups

Calibrated on the dev DB (2026-09-17; prototype in `scratch_group_proto.py`).
Deterministic first, model second, every group gets a `confidence`:

1. **Jane `product_id` (confidence 1.0).** A real catalog key: 27k merges, all correct in samples.
2. **Normalised-name equality (0.95):** identical `(block, name_norm)`. `name_norm` = lowercase, brand tokens removed, size/dose tokens removed (`10pk`, `1g`, `100mg`, `10x10mg`), punctuation collapsed, filler (`rec`, `med`, `the`) dropped — but **parenthetical words kept**: "(Indica)"/"(Hybrid)", "(Black)"/"(Tan)", "(M)"/"(L)" are variants, and stripping them merged them.
3. **Dutchie `libraryProductId` (0.9) — a candidate source, NOT a key.** Dispensaries link items to the library at the product-*line* level ("Incredibles bars", "14er prepacks", Mountain High Suckers of any flavour): of 316k candidate pairs only 4.8k survive. Accept only with same block, name-token Jaccard ≥ 0.5, cosine ≥ 0.90 and no strain conflict.
4. **Model-assisted (0.7–0.9):** FAISS top-30 within the same block; accept cosine ≥ **0.97**, or ≥ 0.92 with Jaccard ≥ **0.8**; strain-conflict veto. Sibling flavours score ~0.958 with Jaccard exactly 0.6, which is why the draft 0.92/0.6 rule was wrong. 1.6k merges, all correct in samples; this tier is what links cross-format and cross-platform twins.
5. **Strain veto** (all fuzzy tiers): if both names state a strain (sativa/indica/hybrid/cbd or the (S)/(I)/(H) markers) and they differ, never merge.
6. **Union-find with the same-dispensary guard**: a merge that would put two rows from one dispensary in a group is refused (25k refusals in the prototype — it's the backstop for every rule above).

**Block** = `(brand_norm, product_type, size_key, pack_count)` where
- `size_key` for Edible/Tincture/Topicals is the **dose** (`100mgTHC`, parsed from `thc_display` or the name) — Dutchie stores a 100 mg drink's *net* weight (1000 mg), Jane its dose (100 mg), so net weight split every cross-platform edible;
- otherwise it is the weight **canonicalised to mg** (`weight_mg`, else the label parsed: `1g`→1000, `1/8oz`→3500, …) rounded to 50 mg; a non-weight label (`24pk`, `single`) is no weight;
- `pack_count` from `10pk`/`20pk`/`4ct` in the name (a 10-pack and a 20-pack are different SKUs).

Prototype result: 11,642 groups at 2+ stores covering 71% of products; median 3 stores, 90th pct 10, max 108 (Wyld Marionberry); 408 groups span both platforms; median in-group price spread 1.07×, top quarter ≥ 1.38×.

**Known gap to close in phase 2 — flower priced by weight.** For products sold in several sizes, `price` is a *from* price whose size varies by store, so the biggest apparent "spreads" ($1–$17 for one strain at 20 stores) are size mismatches, not deals. Group prices must come from the per-weight fields (Jane `price_gram`/`price_eighth_ounce`/…, Dutchie `Prices`×`Options`), keyed by the same canonical size, before flower comparisons can be shown.

Tables:

```
product_groups(product_row INTEGER PRIMARY KEY, group_id INTEGER, confidence REAL, method TEXT)
group_prices(group_id, n_stores, min_price, median_price, avg_price, max_price)   -- view or table
```

Price comparison is **size-aware by construction** (weight is part of the block/key). Show `price_per_gram` / `per_mg` as a secondary line where `weight_mg` exists, for the "is this actually a deal" read across sizes.

### 4. API

`GET /api/products/{id}/detail` →

```json
{
  "product": {…the row…},
  "group": {
    "confidence": 0.95, "n_stores": 12,
    "stats": {"min": 12.0, "median": 18.0, "avg": 17.6, "max": 24.0},
    "this_vs_avg": -1.6, "this_vs_avg_pct": -9.1, "rank": 3,
    "offers": [{"dispensary_display": "...", "price": 12.0, "product_url": "...", "quantity_available": 20}, …]   // sorted by price
  },
  "similar": [{…row…, "score": 0.87}, …]
}
```

Both lists are single indexed lookups; response time is the same as `/api/search`.

### 5. UI

- Card click opens a modal (the card's current link becomes a "View at {dispensary}" button inside it).
- Header: product info. Middle: **price bar** — a horizontal range from group min to max with the average marked and *this* price highlighted, plus the sentence ("$3.50 under the average of 12 dispensaries"). Then the offers list. Bottom: "Similar products" as a row of mini-cards that open the same modal (so you can browse sideways).
- Confidence < 0.9 groups get a small "likely the same product" label rather than being hidden.
- Keyboard: Esc closes; URL hash `#p=<id>` so a product can be linked/shared.

## Phases

| # | Work | Output | Effort |
| --- | --- | --- | --- |
| 0 | `pip install sentence-transformers faiss-cpu` in the search venv; embed 87k products once; eyeball 30 nearest-neighbour lists | go/no-go on MiniLM quality | 1 hr |
| 1 | `build_similarity.py`: embeddings → FAISS → `similar_products` | table in `products_dev.db` | ½ day |
| 2 | Groups: exact keys + deterministic + model-assisted + union-find + same-store guard; `group_prices` | tables + a **precision check**: 100 random cross-store pairs hand-labelled, target ≥ 95% correct on confidence ≥ 0.9 | 1 day |
| 3 | `/api/products/{id}/detail` + modal on the **dev site** (:8001) | reviewable | ½ day |
| 4 | Nightly hook after the import; promote to prod once the modal and the numbers look right | live | ½ day |

Phase 2 is where the time goes, and where the "is this the same product?" judgement lives. Do it against the dev DB with both platforms in it, since cross-platform matching is the hardest case.

## Phase 0 results (2026-09-17)

- MiniLM-L6-v2 on all 87,069 dev rows: **645 s** to embed (batch 64), **46 s** for all-rows top-30 with `IndexFlatIP`. Index 134 MB.
- Same product at other stores scores 0.99–1.00; same brand's sibling products 0.93–0.96; same-category alternatives 0.75–0.90. Cross-platform twins (Dutchie "Next1 Apple Infused Pre-Roll" ↔ Jars Buckley) at 0.93; a genuinely different Jane "Next1 Labs Hybrid [1g]" at 0.815.
- Price spreads the feature will surface, seen directly: Wyld Marionberry $23.99 (Jars 16th) vs $14 (Jars Longmont/Southlands); Purplebee's Blue Dream cart $7 (EDW Aurora) vs $12–15 (9 other stores); Wana Stay Asleep $14 vs $18–19.
- **Build gotcha:** `faiss` and `torch` cannot be imported in one process on macOS (two OpenMP runtimes → segfault, exit 139). The builder must run embedding and indexing as separate processes, or set `KMP_DUPLICATE_LIB_OK=TRUE` (untested).
- Runs under the default `/usr/local/bin/python3`, which already has sentence-transformers/faiss/torch; the search venv stays lean.

## Sizing on the current Mac (8 GB, Intel)

- Embedding 87k rows: ~11 min CPU (measured). Memory ~1 GB during the build.
- FAISS build + all-pairs top-30: < 1 min. Index 134 MB on disk.
- Nightly job total: ~12 min, after the ~57 min of scraping. Fine.
- Runtime: SQLite only. No torch, no FAISS in the uvicorn process.

## Risks and how the plan handles them

- **False "same product" merges** (the damaging failure — a wrong price comparison looks like a lie): exact keys first, weight in the block, dual threshold (embedding *and* token overlap), same-dispensary guard, confidence shown in the UI, precision check before promotion.
- **Weight ambiguity** (Dutchie `weight` is unreliable, `Options` covers 54% on Jane): block on `weight_mg` when both sides have it, else on `weight_label`, else fall back to name tokens (`1g`, `3.5g` in the name).
- **Brand spelling drift** ("Wana" vs "Wana Brands"): `brand_norm` strips corporate suffixes; brand ids (`brandId`, `product_brand_id`) used as a stronger key within each platform.
- **Bundles/multi-packs** ("2 for $30"): excluded from groups when the name has bundle tokens; still eligible as "similar".
- **Daily churn**: rebuild everything from scratch nightly rather than maintaining state; ids are per-day rows anyway. Group ids are not stable across days — if a "price history for this SKU" feature comes later, persist the *key* (brand_norm+name_norm+weight) not the group id.

## How many stores share a product (measured 2026-09-17)

| | Products at 2+ stores | Median stores | 75th pct | 90th | 99th | Max |
| --- | --- | --- | --- | --- | --- | --- |
| Dutchie (`libraryProductId`) | 2,717 | 4 | 8 | 17 | 57 | 85 (Wyld gummies) |
| Jane (`product_id`) | 4,136 | 4 | 8 | 18 | 44 | 49 |

The typical shared product is at ~4 stores; only ~8% are at more than 20.
Decisions that follow:

- **Compare against every dispensary in the index** (187). Group size never
  affects request-time cost (two indexed lookups) or build cost (FAISS asks
  for the 30 nearest, never all pairs), and a distance filter would only
  shrink an already small median group. Distance can be a later toggle.
- **Offers list, two regimes:** up to ~10 offers, list them all sorted by
  price. Beyond that (the Wyld case), show the price bar and stats, the
  cheapest five, this dispensary's rank ("14th of 85"), and a "show all"
  expander.

## Decided

- **Averages are per store** (decided 2026-09-17): one price per (group,
  dispensary) — the store's lowest listing for that SKU — feeds
  `group_prices`. A store listing the same SKU twice counts once.

## Open questions

1. Cross-platform threshold tuning will need real examples — the Wana/Dialed In/Cookies/Wyld brands exist on both platforms and make a good test set.
