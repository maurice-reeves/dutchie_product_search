# Dutchie Product Search

A local search engine over the product data scraped by `dutchie_scraper`
(a separate project). It reads the scraper's output CSV into a SQLite
database with full-text search, then serves a small web UI to search and
filter products by name, brand, dispensary, type, weight and price —
linking each card straight to that product on the dispensary's own menu.

It also tracks **what came back on the shelf**: each import snapshots every
product and diffs against the previous day, so restocked, repackaged and
newly listed items are flagged on the cards.

This project is **standalone**. It only reads a CSV that `dutchie_scraper`
already produced; it does not import or depend on that package.

---

## How it works

```
all_dispensaries*.csv  ─┐   import_products.py        app.py (FastAPI)         static/index.html
(Dutchie stores)        ├──────────────────▶  data/products.db  ──────────▶  /api/*  ──────────▶  browser UI
vireo_products*.csv    ─┘   one products table   products + FTS5            JSON search           vanilla JS
(Vireo's Jane stores)       Jane wins overlaps   + popup tables
```

1. **`import_products.py`** — the nightly import. Maps each scraper's CSV to
   the same ~20 product columns (`import_csv.py` for Dutchie's ~170-column
   CSV, `import_vireo_csv.py` for Jane's), merges them — when a store is on
   both platforms the Jane copy wins — writes `data/products.db`, records
   when each product was first seen, and rebuilds the product popup's
   tables (`build_similarity.py`). See [Combining the two sources](#combining-the-two-sources).
2. **`app.py`** — a FastAPI app that queries `products.db` on every
   request (the DB file is opened fresh per request, so re-importing does
   not require a server restart) and also serves the static frontend.
3. **`static/index.html`** — a single self-contained page (vanilla JS, no
   build step, light/dark aware) that calls the JSON API.

### Data cleaning done during import

| Source field         | Becomes            | Transform                                                        |
| -------------------- | ------------------ | --------------------------------------------------------------- |
| `dispensary`         | `dispensary_display` / `dispensary_slug` | `natures-kiss/products` → `Natures Kiss` / `natures-kiss` |
| `THCContent_range` + `THCContent_unit` | `thc_display` | `[100]`+MILLIGRAMS → `100mg`; `[24.3]`+PERCENTAGE → `24.3%`. The unit matters — most rows are percentages, and assuming mg rendered a 24.3%-THC flower as "24.3mg" |
| `measurements_netWeight_values` | `weight_mg` | first value, always milligrams. **Not** the `weight` column, which reads 1000 for a 3.5g flower and a 100mg drink alike and matches the real net weight only 56.9% of the time |
| `Options`            | `weight_label`     | Dutchie's own label (`3.5g`, `1/8oz`), falling back to formatting `weight_mg` |
| `cName` + `dispensary` | `product_url`    | `https://dutchie.com/dispensary/<slug>/product/<cName>` — the singular `/product/` resolves, the plural `/products/` does not |
| `id`                 | `product_id`       | Dutchie's product id; what lets a product be followed across daily snapshots |
| `Prices`             | `price`            | coerced to a number; rows with no parseable price are dropped   |
| `createdAt`          | `created_at`       | parsed to a timestamp (drives "Newest first" + "Added" labels)  |
| `type`               | `product_type`     | null → `Uncategorized`                                          |

Rows without a `Name`, without an `Image`, or without a parseable price
are excluded from the database.

#### Card titles (`display_name`, `display_detail`)

Menus write names every which way — `Malek's | 1g Live Resin Batter |
Juicy (H)`, `REC: Craft Sour Diesel 510 Cartridge Distillate`, `Wyld Gummies
Hybrid Huckleberry 100mg` — so the card shows a title and a descriptor that
`names.py` pulls apart at import time for both sources:

| Listing name | `display_name` | `display_detail` |
| --- | --- | --- |
| `Malek's \| 1g Live Resin Batter \| Juicy (H)` | Juicy (H) | Live resin batter |
| `REC: Craft Sour Diesel 510 Cartridge Distillate` | Sour Diesel | REC · 510 cartridge distillate |
| `Wyld Gummies Hybrid Huckleberry 100mg` | Huckleberry | Gummies hybrid |
| `Black Maple #22 \| 500MG \| Rosin Cartridge` | Black Maple #22 | Rosin cartridge |

The brand and any sizes are removed, the name is cut into segments at
`|`, ` - `, `:` and brackets, and the segment made mostly of words the
matcher's vocabulary does *not* know (`build_similarity.GENERIC` plus the
extra menu words in `names.DESCRIPTOR`) is the product; the rest is the
descriptor, with the card adding the type and size after it. A
single-segment name is split only when its unknown words sit together at
one end; otherwise it is shown whole rather than scrambled, and the title
is never empty. The listing name stays in `name` for search and for the
popup's "Listed as" column. `python names.py --backfill data/products.db`
adds the two columns to an existing database without touching row ids.

### Database schema (`data/products.db`)

- **`products`** — one row per product, keyed by an integer `id`; includes
  the card's `display_name` / `display_detail` (above).
  Indexed on `price`, `product_type`, `dispensary_display`, `created_at`.
- **`products_fts`** — an FTS5 virtual table over `name`, `brand_name`,
  and `dispensary_display`, contentless (mirrors `products` by rowid).
  Powers the free-text search box with prefix matching.
- **`import_meta`** — a single row: `source_csv` (both CSVs), `row_count`,
  `imported_at`. Shown under the page title as "Refreshed …".
- **`first_seen`** — `(product_id, dispensary_slug) → first_seen`, the date
  a product first turned up in a scrape; never overwritten, survives
  `products` being replaced. Jane gives no creation date, so Jane rows get
  their `created_at` (the card's "Added" line, the "Newest first" sort) from
  here; Dutchie rows keep Dutchie's own `createdAt`.
- **`product_prices`, `product_groups`, `group_prices`, `similar_products`**
  — the product popup's tables, written by `build_similarity.py`
  ([below](#product-popup-same-product-elsewhere-similar-products)).
- **`product_snapshots`** / **`restock_events`** — restock tracking, off by
  default ([below](#restock-tracking-off-by-default)).

### Combining the two sources

Vireo Growth's Colorado chains (The Green Solution, Medicine Man, LivWell,
Star Buds / Standing Akimbo, Every Day Weed, Green Dragon) are moving their
menus from Dutchie to Jane, and during the move a store can be on both —
with the Jane menu the live, fuller one. `import_products.py` therefore
drops a Dutchie store when the Jane CSV has the same store, and prints each
decision:

```
Vireo stores on the Dutchie side (Jane wins where both list the store):
  Tgs Wewatta (Dutchie) -> replaced by The Green Solution - Wewatta (Jane)
  Tgs Malley (Dutchie) kept: no matching Jane store
```

"Same store" means the same brand *and* the same location words: the brand
is recognised from either platform's spelling (`Tgs` / `The Green Solution`,
`EDW` / `Every Day Weed`, `SB` / `Star Buds`, …), then words like
*Denver*, *Rec*, *Med*, *Ave* are ignored and the remaining words of one
name must all appear in the other (`Tgs Edgewater` ↔ `The Green Solution -
Edgewater (20th)`). Stores of any other brand are never touched, and a
Vireo store with no Jane counterpart is kept from Dutchie.

The Vireo CSV is used only if it is from within `VIREO_MAX_AGE_DAYS` (3) of
the Dutchie CSV — the Jane job runs right after the Dutchie one, so an older
file means it failed that day, and the import then says so and carries on
with Dutchie stores only.

### Restock tracking (off by default)

Switched off — nobody was using the badges — but kept intact: the code is
at the bottom of `import_csv.py` (`record_restocks`) plus
`backfill_snapshots.py`, `app.py` still tags results when a `restock_events`
table exists, and the page still shows the badge. `RESTOCK_TRACKING=1` on
the import turns it back on; expect a noisy first day, since the previous
snapshot will be weeks old. While off, `restock_events` is dropped at each
import (it would go stale) and `product_snapshots` is left as it was.

`createdAt` never changes once a product exists, so it identifies new
products but says nothing about restocks. The signals that do are a product
**reappearing after being absent** and **`canonicalPackageId` changing** (a
new physical package on the shelf).

Two details matter for accuracy:

- Only dispensaries scraped on **both** days are compared. Scrape coverage
  varies run to run, and without this restriction a dispensary that simply
  failed to scrape yesterday looks like its whole catalogue restocked.
- Reappearances are split on `createdAt`, so a genuinely new product is not
  reported as a restock.

`quantityAvailable` is capped at 25 by the source data, so `quantity_up` is
a weak signal and ranks last.

Run `backfill_snapshots.py` to load history from CSVs already on disk —
otherwise restock detection has nothing to compare against until the second
daily import.

---

## Setup

Requires Python 3.9+ and (for the public-tunnel feature) `cloudflared`.

> The database is refreshed automatically after each scheduled scrape —
> `dutchie_scraper`'s wrapper runs `import_products.py` once both its jobs
> (Dutchie, then Vireo/Jane) have written their CSVs, and the import then
> rebuilds the product popup's tables (see
> [Keeping the popup's tables fresh](#keeping-the-popups-tables-fresh)). The
> manual steps below are for a first build or an ad-hoc refresh.

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Dependencies: `pandas`, `fastapi`, `uvicorn[standard]`.

---

## Usage

### 1. Build / refresh the database

Auto-discovers the newest `all_dispensaries*.csv` and `vireo_products*.csv`
in the parent `Personal Projects/` directory (where both scrapers write), or
takes explicit paths:

```bash
./.venv/bin/python import_products.py
./.venv/bin/python import_products.py --dutchie ../all_dispensaries20260918_064312.csv --vireo ../vireo_products20260918_071219.csv
./.venv/bin/python import_products.py --no-vireo          # Dutchie stores only
./.venv/bin/python import_products.py --no-similarity     # skip the popup tables (seconds instead of minutes)
```

It prints the row counts, the store-overlap decisions and the final DB
path, then runs `build_similarity.py` for the popup (15–20 more minutes;
`--no-similarity` or `SIMILARITY_PYTHON=` skips it). Re-run this any time
you have a fresh scrape. `PRODUCTS_DB=path` writes somewhere other than
`data/products.db` — the same override `app.py` accepts, so a scratch
database can be built and served without touching the live one:

```bash
PRODUCTS_DB=data/products_dev.db ./.venv/bin/python import_products.py
PRODUCTS_DB=data/products_dev.db ./.venv/bin/uvicorn app:app --port 8001
```

`python import_csv.py [csv]` still works and is the same as `--no-vireo`.

### 2. Start the server

```bash
./.venv/bin/uvicorn app:app --port 8000
# add --reload while developing
```

### 3. Open the UI

<http://127.0.0.1:8000>

Re-importing new data does **not** need a server restart — just refresh
the page.

### 4. Vireo's Jane stores

Vireo Growth's Colorado chains — The Green Solution, Medicine Man, LivWell,
Star Buds / Standing Akimbo, Every Day Weed, Green Dragon — sell through
**Jane** storefronts, not Dutchie. The scraper's
`scripts/scrape_vireo_dispensaries.py` writes their menus to
`vireo_products<stamp>.csv` (~35k products from 52 Denver-area stores), and
`import_products.py` folds that into the same database as the Dutchie
stores (86k products, 182 dispensaries as of Sept 2026). `import_vireo_csv.py`
holds the column mapping.

#### Jane → `products` column mapping

Jane's records are Algolia hits with ~90 fields. The importer maps them onto
the same 22 columns Dutchie fills, normalising vocabularies so the filters
merge. The decisions, for when the mapping needs revisiting:

| `products` column | Jane source | Rule |
| --- | --- | --- |
| `name` | `name` | required — row dropped if missing |
| `price` | `bucket_price` | the "from" price on the menu card; required. Per-weight prices (`price_gram`, `price_eighth_ounce`, …) and specials (`special_price_*`) are in the CSV but unused |
| `image_url` | `image_urls[0]` | **not** required — a Vireo row is kept without an image, unlike Dutchie rows |
| `brand_name` | `brand` | |
| `product_type` | `kind` | `flower`→Flower, `vape`→Vaporizers, `edible`→Edible, `extract`→Concentrate, `pre-roll`→Pre-Rolls, `tincture`→Tincture, `topical`→Topicals, `gear`→Accessories (Clothing→Apparel), `grow`→Seeds (name contains "clone"→Clones), `merch`→Apparel |
| `product_subcategory` | `root_subtype`, refined by `brand_subtype` for edibles | ~70-entry table (`SUBCATEGORY` in the importer) onto Dutchie's slugs, e.g. Disposables→`disposables`, Live Resins→`live-resin`, Infused Packs→`infused-pre-roll-packs`, Patches→`transdermal-patches`, gear Vaporizers→`batteries`, Papers/Cones/Wraps/Tips→`papers-rolling-supplies`. Jane's edible "Candies" is split by `brand_subtype` (Gummies→`gummies`, Chocolates→`chocolates`, Beverages→`drinks`, Confections→`candy`, …). TGS flower price tiers (Silver/Gold/Bronze/Select/Value) and catch-alls (Other, Paraphernalia, Accessories) → blank, like Dutchie's plain flower. Unknown subtypes fall back to a slug of the Jane name |
| `strain_type` | `category` | hybrid/indica/sativa/cbd → Hybrid/Indica/Sativa/High CBD |
| `thc_display` | `percent_thc` **or** `dosage` | flower/vape/extract/pre-roll: `percent_thc` as "24.3%". Edible/tincture/topical: the `dosage` string ("100mg", "100mg CBD/100mg THC") — Jane sometimes stores an edible's *mg* figure in `percent_thc`, which would render as "100%" |
| `weight_label` | `available_weights`, else `amount` | Jane weight keys → Dutchie labels: half gram→`.5g`, gram→`1g`, two gram→`2g`, eighth ounce→`1/8oz`, quarter ounce→`1/4oz`, half ounce→`1/2oz`, ounce→`1oz` (smallest listed weight wins). Un-weighted items use `amount` ("10pk", "1000mg") |
| `weight_mg` | `net_weight_grams` × 1000 | |
| `dispensary_display` / `dispensary_slug` | `storeName` / slug of it | store names come from Jane's own store list (`list_stores`) |
| `dispensary_url` | `url` | `https://www.<brand>/shop/store/<id>/shop-all` |
| `product_url` / `product_slug` | `product_id` + `url_slug` | `https://www.<brand>/shop/products/<product_id>/<url_slug>` |
| `product_id` | `product_id` | stable across days — the key for `first_seen` (and for restock tracking, if enabled) |
| `quantity_available` | `max_cart_quantity` | Jane caps the cart at stock on hand — a proxy, not a count |
| `scrape_date` | `scrapeDate` | |
| `created_at` | `first_seen` table | Jane has no creation date; `import_products.py` fills in the date we first saw the product, so the card's "Added" line and "Newest first" work |
| `updated_at`, `package_id` | — | Jane exposes neither (no *new package* restock signal) |

Dates are written as naive UTC text (`2025-09-22 16:14:38.341000`) for
both sources — the page's date parser doesn't accept a `+00:00` suffix and
would print the raw timestamp on the card.

---

## One-step launcher (`start_search.command`)

Double-click **`start_search.command`** in Finder (it is marked
executable) to bring everything up at once:

1. Starts `uvicorn app:app --port 8000` if it is not already running
   (logs to `logs/uvicorn.log`).
2. Starts a **Cloudflare quick tunnel** if one is not already running
   (logs to `logs/cloudflared.log`).
3. Waits for the public URL, prints both the local and public URLs, and
   copies the public URL to the clipboard (`pbcopy`).

It is safe to double-click again later — it checks for the running
processes (`pgrep`) and will not start duplicates. Closing the Terminal
window that opens does **not** stop the server or tunnel; they keep
running in the background (`nohup` + `disown`).

To stop them:

```bash
pkill -f "uvicorn app:app"
pkill -f "cloudflared tunnel"
```

---

## Sharing it publicly with Cloudflare

The app has no auth and binds to localhost. To let someone else reach it
without deploying anything, the launcher uses a **Cloudflare quick
tunnel** — an ephemeral, zero-config tunnel from a random
`*.trycloudflare.com` hostname to your local port 8000.

### What you need

Install `cloudflared` (the Cloudflare Tunnel client):

```bash
brew install cloudflared
```

No Cloudflare account, login, or DNS setup is required for a quick
tunnel.

### Run it manually

```bash
cloudflared tunnel --url http://localhost:8000
```

`cloudflared` prints a line like:

```
https://random-words-1234.trycloudflare.com
```

Anyone with that URL can use the search UI for as long as the tunnel
process stays running.

### How the launcher handles it

`start_search.command` runs the tunnel through a pseudo-terminal:

```bash
nohup script -q /dev/null cloudflared tunnel --url http://localhost:8000 \
  > logs/cloudflared.log 2>&1 &
```

The `script -q /dev/null` wrapper is deliberate: when `cloudflared`
writes straight to a redirected file it fully buffers stdout, so the URL
line can sit unflushed for a long time. Running it under a pty makes it
behave as if interactive and flush each line immediately, so the script
can `grep` the URL out of the log within a couple of seconds.

### Notes and caveats

- **Ephemeral URL.** Each tunnel run gets a new random hostname. There is
  no way to reserve one on a quick tunnel — for a stable domain you need
  a named tunnel tied to a Cloudflare account and a domain.
- **No access control.** Anyone with the link can see everything. The
  data is scraped public product listings, but treat the URL as
  semi-secret and shut the tunnel down when you are done.
- **Rate limits / longevity.** Quick tunnels are best-effort and intended
  for testing; Cloudflare may throttle or drop long-lived ones.
- **Find the current URL later:**
  ```bash
  grep -oE 'https://[a-zA-Z0-9.-]+\.trycloudflare\.com' logs/cloudflared.log | head -1
  ```

---

## Product popup: same product elsewhere, similar products

Clicking a card opens a popup with the product and **one price list**:
every other dispensary selling the same product at the same size (each
store's lowest price) together with the nearest **similar products**, sorted
by price. Ticked rows make up the range, average, median and the "#n
cheapest" verdict; same-product offers start ticked, similar products start
unticked, and a similar product is priced at the size being compared when
it is sold in that size (otherwise its size cell is flagged `≠`). The data
behind it is built offline by `build_similarity.py`, so the site itself only
reads four extra tables:

```bash
/usr/local/bin/python3 build_similarity.py all --db data/products.db      # ~20 min: embed, index, groups
/usr/local/bin/python3 build_similarity.py groups --db data/products.db   # ~3 min when embeddings exist
```

It runs under the **default** Python (which has `sentence-transformers`,
`faiss-cpu`, `torch`), not the venv. `embed` and `index` are separate
processes on purpose: `torch` and `faiss` each ship an OpenMP runtime and
importing both in one process segfaults on macOS.

### Keeping the popup's tables fresh

The four tables reference products by **row id**, and every import replaces
the `products` table and reassigns the ids. So `import_products.py`:

1. drops the four tables right after writing `products` — the popup then
   shows the product alone (it checks for `product_groups`) instead of
   another row's offers;
2. runs `build_similarity.py all` for the new database once the import is
   done (both CSVs passed along for the per-size prices), under the first
   interpreter it finds that can import both `faiss` and
   `sentence_transformers` (the venv, `python3` on `PATH`, then
   `/usr/local/bin/python3`). `SIMILARITY_PYTHON=/path/to/python` names one
   explicitly; `SIMILARITY_PYTHON=` (empty) or `--no-similarity` skips it.

A failed or skipped rebuild never fails the import — the products are
already on disk — it just leaves the popup in "product only" mode until the
next successful run. Its output goes to the same log as the import, i.e. the
scraper job's log when it runs on the schedule.

| table | what |
| --- | --- |
| `product_prices` | one row per product per size (`size_key`, label, price, sale price) — from Dutchie's `Options`×`recPrices` and Jane's `price_*` columns |
| `product_groups` | the "same product" group each row belongs to, with a `confidence` and the tier (`method`) that placed it |
| `group_prices` | per (group, size): store count and min/median/avg/max of each store's best price |
| `similar_products` | top-12 embedding neighbours per product, distinct products only, own group excluded |

`GET /api/products/{id}/detail` returns the product, its sizes, the offers at
the viewed size (one per store, with `confidence` and `is_this`) and the
similar list. On a database without the tables (production, until the
builder runs there) it returns the product alone and the popup degrades.

### How "same product" is decided

Deterministic first, model second, and every group carries a confidence
(see `PLAN_similar_products.md` for the calibration; 94/100 on a hand-labelled
sample of cross-store pairs). In order:

1. Jane `product_id` — a real catalog key (1.0).
2. Identical normalised name within a **block** = brand + type + size (0.95).
   Sizes/doses and brand words are stripped from the name; parenthetical
   words are kept because "(Indica)"/"(Hybrid)", "(Black)"/"(Tan)" are
   different products. Edibles/tinctures/topicals block on the labelled
   **dose** (Dutchie stores net weight, Jane the dose); everything else on
   weight canonicalised to mg.
3. Dutchie `libraryProductId` — **not** a key (it links product *lines*), so it
   only proposes pairs that still need distinctive-token overlap ≥ 0.8 and
   embedding cosine ≥ 0.90 (0.9).
4. FAISS neighbours in the same block with cosine ≥ 0.92 and distinctive-token
   overlap ≥ 0.8 (confidence = cosine). "Distinctive" = after removing
   category words (live, rosin, cart, gummies, …) — otherwise every Lazercat
   item looks like every other.
5. Group-level vetoes on every merge: never two rows from the same dispensary,
   never two different stated strains, formats (cartridge vs AIO vs kit) or
   pack counts. Checking at the group level is what stops A↔B↔C chains.

Matching is deliberately not the last word: every row in the popup has a
checkbox, ticking or unticking one recomputes the range/average/median and
the ranking live, and the choices are remembered per product in
`localStorage` (key `cmp:<group>:<size>`: same-product rows unticked,
similar rows ticked). Low-confidence offers are labelled "likely the same";
similar rows carry a `similar · 92%` tag and a row's name opens that
product's own popup. On phones the listing name folds under the store name
so the price column stays on screen.

## Status dashboard (`/dash`)

A private page showing the host (CPU, memory, disk, network, uptime), the
scrape (last import, whether a run is in flight via the checkpoint
directory, the latest scraper log line) and live visitors. It is
password-protected with HTTP Basic auth and **does not exist** until a
password is configured — every route 404s otherwise:

```bash
echo 'a-long-password' > .dashboard_password     # git-ignored; or DASHBOARD_PASSWORD=...
# optional: DASHBOARD_USER (default admin), DASHBOARD_SCRAPE_DIR (default: parent dir)
```

Then `/dash` (user `admin`). Visitor counts come from a heartbeat the search
page posts every 10 s carrying only a random per-tab id; views and the peak
persist in `data/visitors.json`. The dashboard lives in `dashboard/` and is
never served from `static/`. Run uvicorn with a single worker — the
counters are in-process.

## HTTP API

All endpoints return JSON. Served by `app.py`.

### `GET /api/meta`

The single `import_meta` row: `{ source_csv, row_count, imported_at }`.

### `GET /api/filters`

Available values for each filter, **narrowed by the other filters**. Pass the
current selections (same repeated-parameter form as `/api/search`) and each
list comes back containing only values that still have matching products —
picking a brand shrinks the weight list to that brand's sizes.

```json
{ "types": [...], "dispensaries": [...], "brands": [...], "weights": [...],
  "min_price": 1.0, "max_price": 900.0 }
```

Each filter is excluded from its own query, so its list keeps offering the
alternatives you could add; otherwise brands would collapse to the one
already picked. `weights` is ordered by real size, parsed from the label —
labels that are not weights (`single`, `5pack`) sort last.

### `GET /api/search`

| Param          | Type   | Default       | Notes                                                            |
| -------------- | ------ | ------------- | -------------------------------------------------------------- |
| `q`            | string | `""`          | free text over name / brand / dispensary (prefix match, FTS5) |
| `product_type` | string[] | —           | repeatable; values OR together                                 |
| `dispensary`   | string[] | —           | repeatable; values OR together                                 |
| `brand`        | string[] | —           | repeatable; values OR together                                 |
| `weight`       | string[] | —           | repeatable; matches `weight_label`                             |
| `min_price`    | float  | —             | `price >= min_price`                                           |
| `max_price`    | float  | —             | `price <= max_price`                                           |
| `sort`         | enum   | `relevance`   | `relevance` \| `price_asc` \| `price_desc` \| `name_asc` \| `newest` |
| `page`         | int    | `1`           | 1-based                                                        |
| `page_size`    | int    | `24`          | 1–100                                                          |

Filters are multi-select: values OR within a filter and AND across filters,
so "Flower or Edible" at "Wyld or PAX" reads the way you would expect.
Results are tagged with `restock_reason` (and, for `quantity_up`, the
quantity pair) when the product changed in the latest scrape.

`relevance` uses the FTS5 `rank` only when `q` is non-empty; otherwise it
falls back to `id` order. Response:

```json
{ "results": [ { "id": 1, "name": "...", "image_url": "...", "price": 42.0, ... } ],
  "total": 1234, "page": 1, "page_size": 24 }
```

---

## Project layout

```
app.py                 FastAPI search API + static file mount (PRODUCTS_DB overrides the DB)
import_products.py     nightly import: both CSVs → data/products.db, first-seen dates, popup tables
names.py               listing name → card title + descriptor (used by the import; --backfill for an existing db)
import_csv.py          Dutchie CSV → product rows (mapping); restock tracking parked at the bottom
import_vireo_csv.py    Vireo/Jane CSV → product rows (mapping)
build_similarity.py    nightly: per-size prices, same-product groups, similar products (default python3)
tests/                 pytest for the matching rules (/usr/local/bin/python3 -m pytest -q tests)
backfill_snapshots.py  load snapshot history from older CSVs (one-off)
dashboard/             private /dash status page: metrics, visitors, auth
start_search.command   double-click launcher: server + Cloudflare tunnel
requirements.txt       pandas, fastapi, uvicorn[standard], psutil
static/index.html      the entire frontend (no build step)
data/products.db       generated; git-ignored (products_dev.db, visitors.json too)
logs/                  uvicorn.log, cloudflared.log; git-ignored
```

## Troubleshooting

- **"Dutchie CSV not found"** — pass the CSV path explicitly:
  `import_products.py --dutchie <csv>` (and `--vireo <csv>`).
- **UI header shows no product count** — `data/products.db` is missing or
  empty; run `import_products.py`.
- **Public URL "not found yet"** — check `logs/cloudflared.log`; the
  tunnel may still be connecting, or `cloudflared` is not installed.
- **Port 8000 already in use** — an old `uvicorn` is still running;
  `pkill -f "uvicorn app:app"`.
- **No restock badges** — restock detection needs two snapshots. Run
  `backfill_snapshots.py` to seed history from CSVs already on disk.
- **Public URL stops working** — quick tunnels are ephemeral and get a new
  random hostname every time `cloudflared` restarts, so any link you shared
  previously is dead. Re-run `start_search.command` for a fresh one.
```
