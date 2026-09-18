"""Build/refresh the local SQLite product database from a scraper output CSV.

Usage:
    python import_csv.py [path/to/all_dispensaries*.csv]

With no argument, auto-discovers the most recently modified
all_dispensaries*.csv in the parent "Personal Projects" directory (where the
dutchie_scraper notebook writes its output).

After the database is written, the product popup's tables (same product at
other stores, similar products) are rebuilt by running build_similarity.py
under an interpreter that has its ML stack -- see refresh_similarity().

Environment:
    PRODUCTS_DB         database to write (default data/products.db; the
                        same override app.py honours)
    SIMILARITY_PYTHON   interpreter for build_similarity.py; set it to an
                        empty string to skip the similarity refresh
"""
import ast
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("PRODUCTS_DB") or PROJECT_ROOT / "data" / "products.db")

# Written by build_similarity.py; read by the product popup (app.py).
SIMILARITY_TABLES = ("product_prices", "product_groups", "group_prices", "similar_products")

# Only pull the columns the search app actually displays/filters on — the
# source CSV has ~170 columns (POS-integration-specific fields that vary per
# dispensary) and can be 100MB+, so there's no reason to load the rest.
USE_COLUMNS = [
    "Name", "Image", "Prices", "brand_name", "type", "subcategory",
    "strainType", "THCContent_range", "dispensary", "url",
    "cName", "scrapeDate", "createdAt",
    # Weight. The bare `weight` column is not usable: it reads 1000 for a 3.5g
    # flower, a 3g pre-roll pack and a 100mg drink alike, and agrees with the
    # real net weight only 56.9% of the time. `measurements_netWeight_values`
    # is a list in milligrams (measurements_netWeight_unit is always
    # MILLIGRAMS), and `Options` carries the label Dutchie shows shoppers
    # ("3.5g", "1/8oz"), which covers slightly more rows.
    "measurements_netWeight_values", "Options",
    # THC figures are not all milligrams — 23,192 rows are PERCENTAGE — so the
    # unit has to travel with the value.
    "THCContent_unit",
    # Restock tracking. `id` is Dutchie's product id and is what lets us follow
    # a product across daily snapshots (the `id` column in the products table is
    # just a synthetic rowid for FTS). `createdAt` never changes, so it cannot
    # detect a restock on its own -- the signals that do are a product
    # reappearing after being absent, and canonicalPackageId changing (a new
    # physical package on the shelf).
    "id", "updatedAt",
    "POSMetaData_canonicalPackageId",
    "POSMetaData_children_quantityAvailable",
]

# How many days of per-product snapshots to retain. 50k products/day, so 30
# days is ~1.5M rows -- trivial for SQLite and plenty for week-over-week views.
SNAPSHOT_RETENTION_DAYS = 30

# A product that reappears with a createdAt newer than this many days is
# treated as genuinely new rather than restocked.
NEW_PRODUCT_WINDOW_DAYS = 2


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


THC_UNIT_SUFFIX = {
    "PERCENTAGE": "%",
    "MILLIGRAMS": "mg",
    "MILLIGRAMS_PER_GRAM": "mg/g",
}


def clean_thc(raw, unit=None) -> str:
    """"[100]" + MILLIGRAMS -> "100mg"; "[24.3]" + PERCENTAGE -> "24.3%".

    The unit matters: most rows are PERCENTAGE, and assuming milligrams
    rendered a 24.3%-THC flower as "24.3mg". An unrecognised or missing unit
    yields a bare number rather than a guessed one.
    """
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
    suffix = THC_UNIT_SUFFIX.get(unit, "")
    if len(nums) == 1:
        return f"{nums[0]}{suffix}"
    return f"{min(nums)}-{max(nums)}{suffix}"


def first_in_list(raw):
    """"[3500]" -> 3500, "['3.5g']" -> "3.5g", "[]" or blank -> None."""
    if isinstance(raw, (int, float)):
        return None if pd.isna(raw) else raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        values = ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw.strip() or None
    if isinstance(values, (list, tuple)):
        return values[0] if values else None
    return values


def format_mg(mg) -> str:
    """3500 -> '3.5g', 100 -> '100mg'. Used only when Options has no label."""
    if mg is None or pd.isna(mg):
        return ""
    mg = float(mg)
    return f"{mg / 1000:g}g" if mg >= 1000 else f"{mg:g}mg"


def product_level(out: pd.DataFrame) -> pd.DataFrame:
    """Collapse to one row per (product_id, dispensary_slug).

    The scraper explodes ``POSMetaData_children``, so a product sold in three
    weights appears three times. Quantity is taken as the **max** across those
    options (the product is on the shelf if any option is) and price as the
    min, which is what a shopper would see as the "from" price.
    """
    have_id = out[out["product_id"].notna() & (out["product_id"].astype(str) != "")]
    return have_id.groupby(["product_id", "dispensary_slug"], as_index=False).agg(
        quantity=("quantity_available", "max"),
        package_id=("package_id", "first"),
        name=("name", "first"),
        brand_name=("brand_name", "first"),
        price=("price", "min"),
        product_type=("product_type", "first"),
        dispensary_display=("dispensary_display", "first"),
        dispensary_url=("dispensary_url", "first"),
        image_url=("image_url", "first"),
        created_at=("created_at", "first"),
        scrape_date=("scrape_date", "first"),
    )


def ensure_snapshot_schema(conn: sqlite3.Connection) -> None:
    """Snapshot history survives the products table being replaced each import."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS product_snapshots (
            product_id      TEXT NOT NULL,
            dispensary_slug TEXT NOT NULL,
            scrape_date     TEXT NOT NULL,
            quantity        REAL,
            package_id      TEXT,
            PRIMARY KEY (product_id, dispensary_slug, scrape_date)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_snapshots_date ON product_snapshots(scrape_date)"
    )


RESTOCK_COLUMNS = [
    "product_id", "name", "brand_name", "price", "product_type", "image_url",
    "dispensary_display", "dispensary_slug", "dispensary_url",
    "created_at", "scrape_date", "prev_scrape_date", "reason",
    "prev_quantity", "quantity", "prev_package_id", "package_id",
]


def compute_restocks(conn: sqlite3.Connection, today: str, snap: pd.DataFrame) -> pd.DataFrame:
    """Classify what landed on shelves today, versus the previous snapshot.

    Only dispensaries scraped on **both** days are considered. Coverage varies
    run to run (a dispensary that fails to parse simply vanishes), and without
    this restriction its entire catalogue looks like it restocked overnight --
    on the 7th->8th comparison that nearly doubled the apparent count.

    Reasons, in priority order:
      returned    -- absent yesterday, back today, with an old createdAt
      new_listing -- absent yesterday, back today, createdAt within the window
      new_package -- same listing, different canonicalPackageId (new package)
      quantity_up -- same listing and package, more units on hand
    """
    row = conn.execute(
        "SELECT MAX(scrape_date) FROM product_snapshots WHERE scrape_date < ?", (today,)
    ).fetchone()
    prev_date = row[0] if row else None
    if not prev_date:
        print("No earlier snapshot to compare against — skipping restock detection.")
        # Same columns as a real result: a column-less frame makes to_sql emit
        # "CREATE TABLE restock_events ()", which SQLite rejects, so a first
        # import into a fresh database used to crash here.
        return pd.DataFrame(columns=RESTOCK_COLUMNS)

    prev = pd.read_sql(
        "SELECT product_id, dispensary_slug, quantity, package_id "
        "FROM product_snapshots WHERE scrape_date = ?",
        conn, params=(prev_date,),
    )

    shared = set(prev["dispensary_slug"]) & set(snap["dispensary_slug"])
    p = prev[prev["dispensary_slug"].isin(shared)]
    s = snap[snap["dispensary_slug"].isin(shared)]
    print(
        f"Comparing against {prev_date}: {len(shared)} dispensaries covered both days "
        f"({len(set(snap['dispensary_slug']) - shared)} only today, "
        f"{len(set(prev['dispensary_slug']) - shared)} only then)"
    )

    merged = s.merge(
        p.rename(columns={"quantity": "prev_quantity", "package_id": "prev_package_id"}),
        on=["product_id", "dispensary_slug"], how="left", indicator=True,
    )

    appeared = merged["_merge"] == "left_only"
    cutoff = pd.Timestamp(today) - pd.Timedelta(days=NEW_PRODUCT_WINDOW_DAYS)
    genuinely_new = appeared & (merged["created_at"] >= cutoff)

    both = merged["_merge"] == "both"
    pkg_changed = (
        both
        & merged["package_id"].notna() & merged["prev_package_id"].notna()
        & (merged["package_id"].astype(str) != merged["prev_package_id"].astype(str))
    )
    qty_up = both & (merged["quantity"] > merged["prev_quantity"])

    merged["reason"] = None
    merged.loc[qty_up, "reason"] = "quantity_up"
    merged.loc[pkg_changed, "reason"] = "new_package"
    merged.loc[appeared, "reason"] = "returned"
    merged.loc[genuinely_new, "reason"] = "new_listing"

    events = merged[merged["reason"].notna()].copy()
    events["scrape_date"] = today
    events["prev_scrape_date"] = prev_date
    return events[RESTOCK_COLUMNS]


def drop_similarity_tables(conn: sqlite3.Connection) -> None:
    """The popup's tables reference products by row id, and replacing the
    products table reassigns every id. Drop them so the popup degrades to
    "product only" (app.py checks for product_groups) rather than showing
    another row's offers until refresh_similarity() rebuilds them."""
    for table in SIMILARITY_TABLES:
        conn.execute(f"DROP TABLE IF EXISTS {table}")


def similarity_python() -> "str | None":
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


def refresh_similarity(csv_path: Path) -> bool:
    """Rebuild the popup's tables for the database just written (~10 min).

    Never fails the import: the products are already on disk, and without
    these tables the popup simply shows the product alone. Returns whether
    the tables were rebuilt.
    """
    python = similarity_python()
    if python is None:
        print("Similarity tables not refreshed: no interpreter with faiss + sentence-transformers "
              "(set SIMILARITY_PYTHON; SIMILARITY_PYTHON= skips this quietly)")
        return False
    cmd = [python, str(PROJECT_ROOT / "build_similarity.py"), "all",
           "--db", str(DB_PATH), "--dutchie-csv", str(csv_path)]
    print(f"Refreshing similarity tables: {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd)     # its progress goes to our stdout, i.e. the job log
    if result.returncode != 0:
        print(f"Similarity refresh failed (exit {result.returncode}); "
              "the popup shows products alone until the next successful run")
    return result.returncode == 0


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
    df["thc_display"] = [
        clean_thc(v, u) for v, u in zip(df["THCContent_range"], df["THCContent_unit"])
    ]

    # Net weight in milligrams, numeric so it can be sorted/filtered on. Zero
    # means "not reported" here, not a zero-weight product.
    df["weight_mg"] = pd.to_numeric(
        df["measurements_netWeight_values"].apply(first_in_list), errors="coerce"
    )
    df.loc[df["weight_mg"] <= 0, "weight_mg"] = None

    # Display label, preferring Dutchie's own ("3.5g", "1/8oz") and falling
    # back to formatting the milligram figure.
    option_labels = df["Options"].apply(first_in_list)
    df["weight_label"] = [
        str(label) if label is not None and str(label).strip().upper() not in ("", "N/A")
        else format_mg(mg)
        for label, mg in zip(option_labels, df["weight_mg"])
    ]
    df["brand_name"] = df["brand_name"].fillna("")
    df["type"] = df["type"].fillna("Uncategorized")
    df["subcategory"] = df["subcategory"].fillna("")
    df["strainType"] = df["strainType"].fillna("")

    # Direct link to the product on the dispensary's own Dutchie menu.
    # Verified against the live site: the singular /product/<cName> resolves
    # (page title reads "<product> at <dispensary> | Dutchie") while the plural
    # /products/<cName> does not. Falls back to the dispensary menu when the
    # slug is missing, so a card always links somewhere useful.
    slug = df["cName"].astype("string").str.strip()
    dispensary_menu = df["dispensary"].apply(dispensary_slug)
    df["product_url"] = [
        f"https://dutchie.com/dispensary/{d}/product/{s}"
        if isinstance(s, str) and s and isinstance(d, str) and d
        else f"https://dutchie.com/dispensary/{d}/products"
        for d, s in zip(dispensary_menu, slug)
    ]

    df["quantity_available"] = pd.to_numeric(
        df.get("POSMetaData_children_quantityAvailable"), errors="coerce"
    )
    # Package ids are opaque identifiers that happen to look numeric ("00866246"),
    # so keep them as strings -- leading zeros are significant.
    df["package_id"] = df.get("POSMetaData_canonicalPackageId").astype("string")
    df["product_id"] = df["id"].astype("string")
    df["updated_at"] = pd.to_datetime(df["updatedAt"], errors="coerce", utc=True)

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
        "product_id", "name", "image_url", "price", "brand_name", "product_type",
        "product_subcategory", "strain_type", "weight_label", "weight_mg", "thc_display",
        "dispensary_display", "dispensary_slug", "dispensary_url", "product_url",
        "product_slug", "scrape_date", "created_at", "updated_at",
        "quantity_available", "package_id",
    ]]

    print(f"{len(out):,} rows have both a name, image, and parseable price")

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        out.to_sql("products", conn, if_exists="replace", index=True, index_label="id")
        drop_similarity_tables(conn)

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

        # --- restock tracking -------------------------------------------------
        ensure_snapshot_schema(conn)
        snap = product_level(out)
        today = str(out["scrape_date"].max())

        events = compute_restocks(conn, today, snap)

        # Written after the comparison, so re-running the import for the same
        # day is idempotent: today's rows are replaced, and the "previous"
        # snapshot is still whichever date came before.
        conn.execute("DELETE FROM product_snapshots WHERE scrape_date = ?", (today,))
        snap.assign(scrape_date=today)[
            ["product_id", "dispensary_slug", "scrape_date", "quantity", "package_id"]
        ].to_sql("product_snapshots", conn, if_exists="append", index=False)

        cutoff = (pd.Timestamp(today) - pd.Timedelta(days=SNAPSHOT_RETENTION_DAYS)).date()
        pruned = conn.execute(
            "DELETE FROM product_snapshots WHERE scrape_date < ?", (str(cutoff),)
        ).rowcount

        events.to_sql("restock_events", conn, if_exists="replace", index=False)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_restock_reason ON restock_events(reason)"
        )
        # /api/search looks events up by product per page of results.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_restock_product "
            "ON restock_events(product_id, dispensary_slug)"
        )
        conn.commit()
    finally:
        conn.close()

    print(f"Wrote {DB_PATH} ({len(out):,} products)")
    print(f"Snapshot recorded for {today}: {len(snap):,} products"
          + (f", pruned {pruned:,} rows older than {SNAPSHOT_RETENTION_DAYS} days" if pruned > 0 else ""))
    if not events.empty:
        counts = events["reason"].value_counts()
        print("Restock events: " + ", ".join(f"{n:,} {r}" for r, n in counts.items()))


if __name__ == "__main__":
    csv_arg = sys.argv[1] if len(sys.argv) > 1 else None
    path = Path(csv_arg).expanduser() if csv_arg else find_latest_csv()
    if not path.exists():
        sys.exit(f"CSV not found: {path}")
    build_database(path)
    refresh_similarity(path)
