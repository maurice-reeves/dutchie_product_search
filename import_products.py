#!/usr/bin/env python3
"""Build the search database from both scrapers' CSVs: Dutchie stores plus
Vireo's Jane stores, in one products table.

    ./.venv/bin/python import_products.py                 # newest CSV of each kind
    ./.venv/bin/python import_products.py --dutchie ../all_dispensaries20260918_064312.csv \\
                                          --vireo ../vireo_products20260918_071219.csv
    ./.venv/bin/python import_products.py --no-vireo      # Dutchie only

The scheduled job runs this once both scrapes have finished. What it does:

1. Maps each CSV to the products columns (import_csv.dutchie_products,
   import_vireo_csv.vireo_products).
2. Drops a Dutchie store when the same store is in the Jane CSV -- Vireo is
   moving its menus from Dutchie to Jane and the Jane menu is the live one.
   "Same store" is the same brand plus the same location words (TGS Wewatta
   vs "The Green Solution - Wewatta"); every drop is printed, and a Vireo
   store with no Jane counterpart is kept.
3. Writes products, its FTS index and import_meta on a *staging* copy of the
   live database (so first_seen / snapshots survive and the public site keeps
   serving the old file), drops the popup's tables (their row ids just
   changed), rebuilds them with build_similarity.py, then atomically replaces
   the live file.
4. Records when each (product, store) was first seen and uses that as
   created_at where the source has no date (Jane), so the card's "Added"
   date works for every store.
5. Splits each listing name into the card's title and descriptor
   (display_name / display_detail; see names.py). The listing name itself
   stays in `name` for search and the popup.

Environment:
    PRODUCTS_DB         database to write (default data/products.db; the
                        same override app.py honours)
    SIMILARITY_PYTHON   interpreter for build_similarity.py; empty = skip
    RESTOCK_TRACKING    set to 1 to record snapshots and restock events
                        (off by default; see import_csv.py)
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd

import import_csv
import import_vireo_csv
from names import split_name

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PRODUCTS_DB") or PROJECT_ROOT / "data" / "products.db")
CSV_DIR = PROJECT_ROOT.parent          # where both scrapers write by default

RESTOCK_TRACKING = os.environ.get("RESTOCK_TRACKING", "").lower() not in ("", "0", "false", "no")

# A Vireo CSV older than this (relative to the Dutchie CSV) is ignored rather
# than imported as if it were current -- the Jane job runs right after the
# Dutchie one, so a gap means it failed that day.
VIREO_MAX_AGE_DAYS = 3

# Written by build_similarity.py; read by the product popup (app.py).
SIMILARITY_TABLES = ("product_prices", "product_groups", "group_prices", "similar_products")


# ----------------------------------------------------------------------------- inputs

def newest(pattern: str, csv_dir: Path) -> Path | None:
    files = sorted(csv_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def csv_stamp(path: Path) -> pd.Timestamp | None:
    """The date in the scraper's file name (all_dispensaries20260918_064312.csv)."""
    m = re.search(r"(\d{8})_\d{6}", path.name)
    return pd.Timestamp(m.group(1)) if m else None


def choose_vireo(vireo: Path | None, dutchie: Path) -> Path | None:
    """Use the Vireo CSV only if it is from (about) the same day as the Dutchie one."""
    if vireo is None:
        print("No vireo_products*.csv found -- importing Dutchie stores only.")
        return None
    v, d = csv_stamp(vireo), csv_stamp(dutchie)
    if v is not None and d is not None and (d - v).days > VIREO_MAX_AGE_DAYS:
        print(f"Ignoring {vireo.name}: {(d - v).days} days older than {dutchie.name} "
              f"(limit {VIREO_MAX_AGE_DAYS}) -- importing Dutchie stores only.")
        return None
    return vireo


# ----------------------------------------------------------------------------- combining

# Brand aliases: how each Vireo chain names itself on Dutchie and on Jane.
BRANDS = [
    ("tgs", re.compile(r"^(the green solution|tgs)\b")),
    ("edw", re.compile(r"^(every ?day weed|edw)\b")),
    ("sb", re.compile(r"^(star ?buds|sb)\b")),
    ("livwell", re.compile(r"^liv ?well\b")),
    ("green dragon", re.compile(r"^green dragon\b")),
    ("medicine man", re.compile(r"^medicine man\b")),
    ("akimbo", re.compile(r"^standing akimbo\b")),
]
# Words that say nothing about *which* store it is.
NOISE = {"the", "at", "on", "of", "and", "in", "denver", "colorado", "co", "rec", "recreational",
         "med", "medical", "dispensary", "store", "ave", "avenue", "blvd", "boulevard", "st",
         "street", "rd", "road", "dr", "drive", "pkwy", "parkway", "hwy", "highway"}


def store_key(name: str) -> tuple[str, frozenset[str]] | None:
    """(brand, location words) for a Vireo store name, None for any other store."""
    text = re.sub(r"[^a-z0-9 ]+", " ", str(name).lower())
    for brand, pattern in BRANDS:
        m = pattern.match(text)
        if m:
            words = frozenset(w for w in text[m.end():].split() if w not in NOISE)
            return brand, words
    return None


def same_store(a: tuple, b: tuple) -> bool:
    (brand_a, words_a), (brand_b, words_b) = a, b
    return brand_a == brand_b and bool(words_a) and bool(words_b) and (words_a <= words_b or words_b <= words_a)


def combine(dutchie: pd.DataFrame, jane: pd.DataFrame | None) -> tuple[pd.DataFrame, list[str], int]:
    """One frame with both sources; Jane wins where a store is on both. Returns
    the frame, the lines to print (one per Vireo store on the Dutchie side,
    dropped or kept) and how many Dutchie rows were dropped."""
    if jane is None or jane.empty:
        return dutchie.reset_index(drop=True), [], 0
    jane_keys = {store: store_key(store) for store in jane["dispensary_display"].unique()}
    jane_keys = {s: k for s, k in jane_keys.items() if k}
    lines, dropped = [], set()
    for store in sorted(dutchie["dispensary_display"].unique()):
        key = store_key(store)
        if key is None:
            continue
        matches = sorted(s for s, k in jane_keys.items() if same_store(key, k))
        if matches:
            dropped.add(store)
            lines.append(f"  {store} (Dutchie) -> replaced by {', '.join(matches)} (Jane)")
        else:
            lines.append(f"  {store} (Dutchie) kept: no matching Jane store")
    kept = dutchie[~dutchie["dispensary_display"].isin(dropped)]
    out = pd.concat([kept, jane], ignore_index=True)
    # Same naive-UTC text for every row ("2025-09-22 16:14:38.341000"); the
    # page's date parser doesn't understand a "+00:00" suffix.
    for col in ("created_at", "updated_at"):
        out[col] = pd.to_datetime(out[col], errors="coerce", utc=True).dt.tz_localize(None)
    return out, lines, len(dutchie) - len(kept)


# ----------------------------------------------------------------------------- database

def drop_similarity_tables(conn: sqlite3.Connection) -> None:
    """The popup's tables reference products by row id, and replacing the
    products table reassigns every id. Drop them so the popup degrades to
    "product only" (app.py checks for product_groups) rather than showing
    another row's offers until refresh_similarity() rebuilds them."""
    for table in SIMILARITY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def record_first_seen(conn: sqlite3.Connection, seen_on: str) -> int:
    """Remember the first date each (product, store) turned up in a scrape and
    fill created_at from it where the source gave none. Returns how many
    products were seen for the first time."""
    conn.execute("""CREATE TABLE IF NOT EXISTS first_seen (
                        product_id TEXT NOT NULL, dispensary_slug TEXT NOT NULL, first_seen TEXT NOT NULL,
                        PRIMARY KEY (product_id, dispensary_slug))""")
    new = conn.execute("""INSERT OR IGNORE INTO first_seen (product_id, dispensary_slug, first_seen)
                          SELECT DISTINCT product_id, dispensary_slug, ? FROM products
                          WHERE product_id IS NOT NULL AND dispensary_slug IS NOT NULL""", (seen_on,)).rowcount
    conn.execute("""UPDATE products SET created_at = (
                        SELECT f.first_seen || ' 00:00:00' FROM first_seen f
                        WHERE f.product_id = products.product_id AND f.dispensary_slug = products.dispensary_slug)
                    WHERE created_at IS NULL""")
    return new


def _sidecars(path: Path):
    for suffix in ("-wal", "-shm", "-journal"):
        yield Path(str(path) + suffix)


def unlink_sidecars(path: Path) -> None:
    """SQLite names WAL/SHM/journal files after the db path. Leftovers from a
    previous inode at that path would attach to a newly published file."""
    for sidecar in _sidecars(path):
        sidecar.unlink(missing_ok=True)


def staging_path(live: Path) -> Path:
    return live.with_name(live.name + ".staging")


def prepare_staging(live: Path) -> Path:
    """A private copy of the live database to build into.

    first_seen and product_snapshots have to survive a products rebuild, so
    the staging file starts as a consistent snapshot of live (sqlite backup,
    which is safe while the site is reading). No live file means a fresh
    staging path and write_database creates it.
    """
    staging = staging_path(live)
    staging.unlink(missing_ok=True)
    unlink_sidecars(staging)
    if live.exists() and live.stat().st_size > 0:
        # as_uri percent-encodes spaces (the parent folder is Personal Projects).
        src = sqlite3.connect(live.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dst = sqlite3.connect(staging)
            src.backup(dst)
            dst.close()
        finally:
            src.close()
    return staging


def publish_database(staging: Path, live: Path) -> None:
    """Atomically put staging at live.

    os.replace keeps connections that already opened the old inode on the
    previous catalogue; app.py opens the path per request, so the next hit
    sees the new file. Sidecars are named after the live path and would
    attach to the new inode, so they go.
    """
    live.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, live)
    unlink_sidecars(live)
    unlink_sidecars(staging)


def write_database(out: pd.DataFrame, sources: list[Path], dest: Path | None = None) -> list[str]:
    """Replace the products table and everything derived from it. Returns the
    lines to print. `dest` is the file to write (the nightly job passes a
    staging copy so the live file stays intact until publish_database)."""
    dest = dest or DB_PATH
    lines = []
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(dest)
    try:
        out.to_sql("products", conn, if_exists="replace", index=True, index_label="id")
        drop_similarity_tables(conn)
        conn.execute("DROP TABLE IF EXISTS products_fts")
        conn.execute("""CREATE VIRTUAL TABLE products_fts USING fts5(
                            name, brand_name, dispensary_display, content='products', content_rowid='id')""")
        conn.execute("""INSERT INTO products_fts(rowid, name, brand_name, dispensary_display)
                        SELECT id, name, brand_name, dispensary_display FROM products""")
        for col in ("price", "product_type", "dispensary_display", "created_at"):
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_products_{col} ON products({col})")
        conn.execute("CREATE TABLE IF NOT EXISTS import_meta (source_csv TEXT, row_count INTEGER, imported_at TEXT)")
        conn.execute("DELETE FROM import_meta")
        conn.execute("INSERT INTO import_meta VALUES (?, ?, datetime('now'))",
                     (" + ".join(str(p) for p in sources), len(out)))

        seen_on = str(pd.to_datetime(out["scrape_date"]).max().date())
        new = record_first_seen(conn, seen_on)
        lines.append(f"First seen on {seen_on}: {new:,} products")

        if RESTOCK_TRACKING:
            lines += import_csv.record_restocks(conn, out)
        else:
            # Derived from day-to-day snapshots; left in place it would tag
            # results with stale reasons. Snapshots themselves are kept.
            conn.execute("DROP TABLE IF EXISTS restock_events")
            lines.append("Restock tracking off (RESTOCK_TRACKING=1 turns it on)")
        conn.commit()
    finally:
        conn.close()
    return lines


# ----------------------------------------------------------------------------- similarity

def similarity_python() -> str | None:
    """An interpreter that can import build_similarity.py's ML stack.

    The venv deliberately doesn't carry sentence-transformers/faiss/torch
    (~1 GB); on this machine they live in the default python.org install.
    SIMILARITY_PYTHON names one explicitly; an empty value means "skip".
    The two imports are probed separately because torch and faiss cannot
    share a process on macOS (see build_similarity.py).
    """
    explicit = os.environ.get("SIMILARITY_PYTHON")
    if explicit is not None:
        return explicit or None
    for candidate in (sys.executable, shutil.which("python3"), "/usr/local/bin/python3"):
        if not candidate or not Path(candidate).exists():
            continue
        if all(subprocess.run([candidate, "-c", f"import {module}"], capture_output=True).returncode == 0
               for module in ("faiss", "sentence_transformers")):
            return candidate
    return None


def refresh_similarity(dutchie_csv: Path, vireo_csv: Path | None, db: Path | None = None) -> bool:
    """Rebuild the popup's tables for the database just written (10-20 min).

    Never fails the import: the products are already on disk, and without
    these tables the popup simply shows the product alone. Returns whether
    the tables were rebuilt. `db` is the file to write (staging, during the
    nightly job) so the live site is untouched until publish_database.
    """
    dest = db or DB_PATH
    python = similarity_python()
    if python is None:
        print("Similarity tables not refreshed: no interpreter with faiss + sentence-transformers "
              "(set SIMILARITY_PYTHON; SIMILARITY_PYTHON= skips this quietly)")
        return False
    cmd = [python, str(PROJECT_ROOT / "build_similarity.py"), "all",
           "--db", str(dest), "--dutchie-csv", str(dutchie_csv)]
    if vireo_csv is not None:
        cmd += ["--vireo-csv", str(vireo_csv)]
    print(f"Refreshing similarity tables: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd)     # its progress goes to our stdout, i.e. the job log
    if result.returncode != 0:
        print(f"Similarity refresh failed (exit {result.returncode}); "
              "the popup shows products alone until the next successful run")
    return result.returncode == 0


# ----------------------------------------------------------------------------- entry point

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dutchie", type=Path, help="Dutchie CSV (default: newest all_dispensaries*.csv)")
    ap.add_argument("--vireo", type=Path, help="Vireo CSV (default: newest vireo_products*.csv, if recent)")
    ap.add_argument("--no-vireo", action="store_true", help="Dutchie stores only")
    ap.add_argument("--csv-dir", type=Path, default=CSV_DIR, help="where to look for the CSVs")
    ap.add_argument("--no-similarity", action="store_true", help="skip the popup tables (same as SIMILARITY_PYTHON=)")
    a = ap.parse_args(argv)

    dutchie_csv = a.dutchie or newest("all_dispensaries*.csv", a.csv_dir)
    if dutchie_csv is None or not dutchie_csv.exists():
        sys.exit(f"Dutchie CSV not found: {dutchie_csv or a.csv_dir / 'all_dispensaries*.csv'}")
    vireo_csv = None if a.no_vireo else choose_vireo(a.vireo or newest("vireo_products*.csv", a.csv_dir), dutchie_csv)
    if vireo_csv is not None and not vireo_csv.exists():
        sys.exit(f"Vireo CSV not found: {vireo_csv}")

    dutchie = import_csv.dutchie_products(dutchie_csv)
    jane = import_vireo_csv.vireo_products(vireo_csv) if vireo_csv else None
    out, decisions, dropped = combine(dutchie, jane)
    out["display_name"], out["display_detail"] = zip(*(split_name(n, b) for n, b in zip(out["name"], out["brand_name"])))
    if decisions:
        print("Vireo stores on the Dutchie side (Jane wins where both list the store):")
        print("\n".join(decisions))
    sources = [dutchie_csv] + ([vireo_csv] if vireo_csv else [])

    # Build on a copy so the public site keeps serving the previous catalogue
    # (and its popup tables) for the 15–20 min similarity rebuild. One
    # os.replace at the end is the cutover.
    staging = prepare_staging(DB_PATH)
    published = False
    try:
        for line in write_database(out, sources, dest=staging):
            print(line)
        if not a.no_similarity:
            refresh_similarity(dutchie_csv, vireo_csv, db=staging)
        publish_database(staging, DB_PATH)
        published = True
    finally:
        if not published:
            staging.unlink(missing_ok=True)
            unlink_sidecars(staging)

    print(f"Wrote {DB_PATH}: {len(out):,} products from {out['dispensary_display'].nunique()} dispensaries "
          f"({len(dutchie) - dropped:,} Dutchie rows, {dropped:,} dropped in favour of Jane; "
          f"{0 if jane is None else len(jane):,} Jane rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
