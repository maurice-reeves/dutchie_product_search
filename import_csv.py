"""Build/refresh the local SQLite product database from a scraper output CSV.

Usage:
    python import_csv.py [path/to/all_dispensaries*.csv]

With no argument, auto-discovers the most recently modified
all_dispensaries*.csv in the parent "Personal Projects" directory (where the
dutchie_scraper notebook writes its output).
"""
import ast
import re
import sqlite3
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"

# Only pull the columns the search app actually displays/filters on — the
# source CSV has ~170 columns (POS-integration-specific fields that vary per
# dispensary) and can be 100MB+, so there's no reason to load the rest.
USE_COLUMNS = [
    "Name", "Image", "Prices", "brand_name", "type", "subcategory",
    "strainType", "weight", "THCContent_range", "dispensary", "url",
    "cName", "scrapeDate", "createdAt",
]


def find_latest_csv() -> Path:
    candidates = sorted(
        PROJECT_ROOT.parent.glob("all_dispensaries*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        raise FileNotFoundError(
            f"No all_dispensaries*.csv files found in {PROJECT_ROOT.parent}"
        )
    return candidates[0]


def clean_dispensary_name(raw: str) -> str:
    """'natures-kiss/products' -> 'Natures Kiss'"""
    if not isinstance(raw, str):
        return ""
    slug = raw.split("/products")[0].strip("/")
    return slug.replace("-", " ").replace("_", " ").title()


def dispensary_slug(raw: str) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.split("/products")[0].strip("/")


def clean_thc(raw) -> str:
    """"[100]" -> "100mg", "[10, 20]" -> "10-20mg" """
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        values = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return re.sub(r"[\[\]']", "", raw)
    if not isinstance(values, (list, tuple)) or not values:
        return ""
    nums = [v for v in values if v is not None]
    if not nums:
        return ""
    if len(nums) == 1:
        return f"{nums[0]}mg"
    return f"{min(nums)}-{max(nums)}mg"


def build_database(csv_path: Path) -> None:
    print(f"Reading {csv_path} ...")
    df = pd.read_csv(csv_path, usecols=lambda c: c in USE_COLUMNS, low_memory=False)
    print(f"Loaded {len(df):,} rows")

    df = df[df["Name"].notna() & df["Image"].notna()].copy()

    df["price"] = pd.to_numeric(df["Prices"], errors="coerce")
    df = df[df["price"].notna()]

    df["created_at"] = pd.to_datetime(df["createdAt"], errors="coerce")

    df["dispensary_display"] = df["dispensary"].apply(clean_dispensary_name)
    df["dispensary_slug"] = df["dispensary"].apply(dispensary_slug)
    df["thc_display"] = df["THCContent_range"].apply(clean_thc)
    df["brand_name"] = df["brand_name"].fillna("")
    df["type"] = df["type"].fillna("Uncategorized")
    df["subcategory"] = df["subcategory"].fillna("")
    df["strainType"] = df["strainType"].fillna("")

    out = df.rename(columns={
        "Name": "name",
        "Image": "image_url",
        "url": "dispensary_url",
        "cName": "product_slug",
        "scrapeDate": "scrape_date",
        "type": "product_type",
        "subcategory": "product_subcategory",
        "strainType": "strain_type",
    })[[
        "name", "image_url", "price", "brand_name", "product_type",
        "product_subcategory", "strain_type", "weight", "thc_display",
        "dispensary_display", "dispensary_slug", "dispensary_url",
        "product_slug", "scrape_date", "created_at",
    ]]

    print(f"{len(out):,} rows have both a name, image, and parseable price")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        out.to_sql("products", conn, if_exists="replace", index=True, index_label="id")

        conn.execute("DROP TABLE IF EXISTS products_fts")
        conn.execute("""
            CREATE VIRTUAL TABLE products_fts USING fts5(
                name, brand_name, dispensary_display,
                content='products', content_rowid='id'
            )
        """)
        conn.execute("""
            INSERT INTO products_fts(rowid, name, brand_name, dispensary_display)
            SELECT id, name, brand_name, dispensary_display FROM products
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_price ON products(price)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_type ON products(product_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_dispensary ON products(dispensary_display)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_products_created_at ON products(created_at)")

        conn.execute("""
            CREATE TABLE IF NOT EXISTS import_meta (
                source_csv TEXT, row_count INTEGER, imported_at TEXT
            )
        """)
        conn.execute("DELETE FROM import_meta")
        conn.execute(
            "INSERT INTO import_meta VALUES (?, ?, datetime('now'))",
            (str(csv_path), len(out)),
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Wrote {DB_PATH} ({len(out):,} products)")


if __name__ == "__main__":
    csv_arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = Path(csv_arg).expanduser() if csv_arg else find_latest_csv()
    if not path.exists():
        sys.exit(f"CSV not found: {path}")
    build_database(path)
