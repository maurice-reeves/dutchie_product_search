"""Backfill product_snapshots from scraper CSVs already on disk.

Restock detection compares today's snapshot against the previous one, so a
freshly built database has nothing to compare against until the second daily
import. This loads the historical `all_dispensaries*.csv` files so the feature
works from the start.

Only the five columns the snapshot needs are read, so this is much faster than
a full import even though the CSVs are ~170MB each.

Usage:
    python backfill_snapshots.py                # every CSV in the parent dir
    python backfill_snapshots.py path/to/a.csv path/to/b.csv

Safe to re-run: each date's rows are replaced, not duplicated.
"""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from import_csv import (
    PROJECT_ROOT,
    SNAPSHOT_RETENTION_DAYS,
    dispensary_slug,
    ensure_snapshot_schema,
)
from import_products import DB_PATH

SNAPSHOT_COLUMNS = [
    "id", "dispensary", "scrapeDate",
    "POSMetaData_children_quantityAvailable",
    "POSMetaData_canonicalPackageId",
]


def snapshot_from_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, usecols=lambda c: c in SNAPSHOT_COLUMNS, low_memory=False)
    df = df[df["id"].notna()]
    df["product_id"] = df["id"].astype("string")
    # Older CSVs store 'name/products'; newer ones store 'name'. dispensary_slug
    # normalises both.
    df["dispensary_slug"] = df["dispensary"].apply(dispensary_slug)
    df["quantity"] = pd.to_numeric(
        df.get("POSMetaData_children_quantityAvailable"), errors="coerce"
    )
    df["package_id"] = df.get("POSMetaData_canonicalPackageId").astype("string")

    # Match import_csv.product_level: one row per product per dispensary, with
    # quantity as the max across exploded child options.
    snap = df.groupby(["product_id", "dispensary_slug"], as_index=False).agg(
        quantity=("quantity", "max"),
        package_id=("package_id", "first"),
        scrape_date=("scrapeDate", "first"),
    )
    return snap[["product_id", "dispensary_slug", "scrape_date", "quantity", "package_id"]]


def main(paths: list) -> int:
    if not paths:
        paths = sorted(PROJECT_ROOT.parent.glob("all_dispensaries*.csv"))
    if not paths:
        sys.exit(f"No all_dispensaries*.csv found in {PROJECT_ROOT.parent}")

    if not DB_PATH.exists():
        sys.exit(f"{DB_PATH} does not exist — run import_products.py first.")

    conn = sqlite3.connect(DB_PATH)
    try:
        ensure_snapshot_schema(conn)
        for path in paths:
            snap = snapshot_from_csv(Path(path))
            if snap.empty:
                print(f"{Path(path).name}: no usable rows, skipped")
                continue
            date = str(snap["scrape_date"].max())
            snap["scrape_date"] = date
            conn.execute("DELETE FROM product_snapshots WHERE scrape_date = ?", (date,))
            snap.to_sql("product_snapshots", conn, if_exists="append", index=False)
            print(f"{Path(path).name}: {len(snap):,} products recorded for {date}")

        latest = conn.execute("SELECT MAX(scrape_date) FROM product_snapshots").fetchone()[0]
        if latest:
            cutoff = (pd.Timestamp(latest) - pd.Timedelta(days=SNAPSHOT_RETENTION_DAYS)).date()
            n = conn.execute(
                "DELETE FROM product_snapshots WHERE scrape_date < ?", (str(cutoff),)
            ).rowcount
            if n:
                print(f"pruned {n:,} rows older than {SNAPSHOT_RETENTION_DAYS} days")
        conn.commit()

        print("\nsnapshot dates now held:")
        for d, n in conn.execute(
            "SELECT scrape_date, COUNT(*) FROM product_snapshots "
            "GROUP BY scrape_date ORDER BY scrape_date"
        ):
            print(f"  {d}  {n:,} products")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
