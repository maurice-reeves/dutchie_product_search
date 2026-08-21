# Dutchie Product Search

A local search engine over the product data scraped by `dutchie_scraper`
(a separate project). Reads the scraper's output CSV into a SQLite database
with full-text search, then serves a small web UI to search/filter products
by name, brand, dispensary, type, and price, showing each product's image.

This project is standalone — it only reads a CSV that `dutchie_scraper`
already produced; it doesn't import or depend on that package.

## Setup

```bash
python -m venv .venv
./.venv/bin/pip install -r requirements.txt
```

## Usage

1. Build the database from the most recent scrape output
   (auto-discovers the newest `all_dispensaries*.csv` in the parent
   `Personal Projects/` directory, or pass a path explicitly):

   ```bash
   ./.venv/bin/python import_csv.py
   # or: ./.venv/bin/python import_csv.py /path/to/all_dispensaries....csv
   ```

2. Start the server:

   ```bash
   ./.venv/bin/uvicorn app:app --port 8000
   ```

3. Open http://127.0.0.1:8000 in a browser.

Re-run step 1 any time you scrape new data, then restart the server (or hit
refresh — the DB file is read fresh on each request).
