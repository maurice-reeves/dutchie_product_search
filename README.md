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
all_dispensaries*.csv        import_csv.py            app.py (FastAPI)         static/index.html
(scraper output, ~170 cols) ───────────────▶  data/products.db  ──────────▶  /api/*  ──────────▶  browser UI
                             extract 15 cols   products + FTS5              JSON search           vanilla JS
```

1. **`import_csv.py`** — reads the CSV with pandas, keeps only the ~15
   columns the UI needs (the source CSV has ~170 POS-specific columns and
   can be 100 MB+), cleans a few fields, and writes `data/products.db`.
   It also records a daily snapshot and computes restock events (below).
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

### Database schema (`data/products.db`)

- **`products`** — one row per product, keyed by an integer `id`.
  Indexed on `price`, `product_type`, `dispensary_display`, `created_at`.
- **`products_fts`** — an FTS5 virtual table over `name`, `brand_name`,
  and `dispensary_display`, contentless (mirrors `products` by rowid).
  Powers the free-text search box with prefix matching.
- **`import_meta`** — a single row: `source_csv`, `row_count`,
  `imported_at`. Shown under the page title as "Refreshed …".
- **`product_snapshots`** — append-only, one row per product per scrape
  date (`product_id`, `dispensary_slug`, `scrape_date`, `quantity`,
  `package_id`). Survives `products` being replaced on each import, and is
  pruned to 30 days. This is what makes restock detection possible.
- **`restock_events`** — rebuilt each import: what changed versus the
  previous snapshot, classified `returned` / `new_listing` /
  `new_package` / `quantity_up`.

### Restock tracking

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
> `dutchie_scraper` runs `import_csv.py` itself once its CSV is written. The
> manual steps below are for a first build or an ad-hoc refresh.

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

Dependencies: `pandas`, `fastapi`, `uvicorn[standard]`.

---

## Usage

### 1. Build / refresh the database

Auto-discovers the most recently modified `all_dispensaries*.csv` in the
parent `Personal Projects/` directory (where the scraper writes its
output), or takes an explicit path:

```bash
./.venv/bin/python import_csv.py
# or
./.venv/bin/python import_csv.py /path/to/all_dispensaries_2026-08-27.csv
```

It prints the row counts at each stage and the final DB path. Re-run this
any time you have a fresh scrape.

### 2. Start the server

```bash
./.venv/bin/uvicorn app:app --port 8000
# add --reload while developing
```

### 3. Open the UI

<http://127.0.0.1:8000>

Re-importing new data does **not** need a server restart — just refresh
the page.

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
app.py                 FastAPI search API + static file mount
import_csv.py          CSV → SQLite builder (run to (re)build the DB)
backfill_snapshots.py  load snapshot history from older CSVs (one-off)
start_search.command   double-click launcher: server + Cloudflare tunnel
requirements.txt       pandas, fastapi, uvicorn[standard]
static/index.html      the entire frontend (no build step)
data/products.db       generated; git-ignored
logs/                  uvicorn.log, cloudflared.log; git-ignored
```

## Troubleshooting

- **"No all_dispensaries*.csv files found"** — pass the CSV path
  explicitly as an argument to `import_csv.py`.
- **UI header shows no product count** — `data/products.db` is missing or
  empty; run `import_csv.py`.
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
