"""Search API + static frontend for scraped Dutchie product data.

Run with:
    ./.venv/bin/uvicorn app:app --reload --port 8000
Then open http://127.0.0.1:8000
"""
import re
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "data" / "products.db"

app = FastAPI(title="Dutchie Product Search")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/meta")
def meta():
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM import_meta LIMIT 1").fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


_WEIGHT_PATTERN = re.compile(r"^\s*(\d+(?:\.\d+)?|\d+/\d+)\s*(g|mg|oz|lb)\s*$", re.I)
_WEIGHT_IN_MG = {"mg": 1.0, "g": 1000.0, "oz": 28349.5, "lb": 453592.0}


def weight_sort_key(label: str):
    """Order weight labels by actual size rather than alphabetically.

    Parsed from the label itself, not the weight_mg column: the two disagree
    (weight_mg for a "1g" row can read 1), because the label is per option
    while the milligram figure is per row. Labels that aren't weights at all
    ("single", "5pack") sort last, alphabetically among themselves.
    """
    m = _WEIGHT_PATTERN.match(label or "")
    if not m:
        return (1, 0.0, (label or "").lower())
    amount, unit = m.group(1), m.group(2).lower()
    if "/" in amount:
        num, den = amount.split("/")
        value = float(num) / float(den)
    else:
        value = float(amount)
    return (0, value * _WEIGHT_IN_MG[unit], "")


@app.get("/api/filters")
def filters():
    conn = get_conn()
    try:
        types = [r[0] for r in conn.execute(
            "SELECT DISTINCT product_type FROM products ORDER BY product_type"
        )]
        dispensaries = [r[0] for r in conn.execute(
            "SELECT DISTINCT dispensary_display FROM products ORDER BY dispensary_display"
        )]
        brands = [r[0] for r in conn.execute(
            "SELECT DISTINCT brand_name FROM products "
            "WHERE brand_name != '' ORDER BY brand_name COLLATE NOCASE"
        )]
        weights = sorted(
            (r[0] for r in conn.execute(
                "SELECT DISTINCT weight_label FROM products WHERE weight_label != ''"
            )),
            key=weight_sort_key,
        )
        price_row = conn.execute("SELECT MIN(price), MAX(price) FROM products").fetchone()
        return {
            "types": types,
            "dispensaries": dispensaries,
            "brands": brands,
            "weights": weights,
            "min_price": price_row[0],
            "max_price": price_row[1],
        }
    finally:
        conn.close()


def attach_restock_flags(conn: sqlite3.Connection, results: list) -> None:
    """Tag each result with how it changed in the latest scrape, if at all.

    Done as a second lookup over just the page of results rather than a JOIN in
    the search query: `restock_events` shares a dozen column names with
    `products` (name, price, dispensary_display, ...), so joining would make
    every reference in the WHERE clause ambiguous for no real gain.

    Adds `restock_reason` (and the quantity pair for "more stock") to each row.
    Absent table or no match simply leaves the fields off.
    """
    if not results:
        return
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='restock_events'"
    ).fetchone()
    if not exists:
        return

    ids = sorted({r["product_id"] for r in results if r.get("product_id")})
    if not ids:
        return

    placeholders = ",".join("?" * len(ids))
    lookup = {
        (r["product_id"], r["dispensary_slug"]): r
        for r in conn.execute(
            f"""SELECT product_id, dispensary_slug, reason, prev_quantity, quantity
                FROM restock_events WHERE product_id IN ({placeholders})""",
            ids,
        )
    }
    for row in results:
        hit = lookup.get((row.get("product_id"), row.get("dispensary_slug")))
        if not hit:
            continue
        row["restock_reason"] = hit["reason"]
        if hit["reason"] == "quantity_up":
            row["restock_prev_quantity"] = hit["prev_quantity"]
            row["restock_quantity"] = hit["quantity"]


@app.get("/api/search")
def search(
    q: str = Query("", description="Free-text search over name/brand/dispensary"),
    product_type: Optional[str] = None,
    dispensary: Optional[str] = None,
    brand: Optional[str] = None,
    weight: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    sort: str = Query("relevance", pattern="^(relevance|price_asc|price_desc|name_asc|newest)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(24, ge=1, le=100),
):
    conn = get_conn()
    try:
        where = []
        params: list = []

        # Every column is qualified with `products.`: the relevance branch
        # joins products_fts, which has its own name / brand_name /
        # dispensary_display columns, so a bare reference is ambiguous and
        # SQLite rejects the whole query.
        if q.strip():
            where.append(
                "products.id IN (SELECT rowid FROM products_fts WHERE products_fts MATCH ?)"
            )
            params.append(f'"{q.strip()}"*')

        if product_type:
            where.append("products.product_type = ?")
            params.append(product_type)
        if dispensary:
            where.append("products.dispensary_display = ?")
            params.append(dispensary)
        if brand:
            where.append("products.brand_name = ?")
            params.append(brand)
        if weight:
            where.append("products.weight_label = ?")
            params.append(weight)
        if min_price is not None:
            where.append("products.price >= ?")
            params.append(min_price)
        if max_price is not None:
            where.append("products.price <= ?")
            params.append(max_price)

        where_clause = f"WHERE {' AND '.join(where)}" if where else ""

        order = {
            "relevance": "products.id" if not q.strip() else "rank",
            "price_asc": "products.price ASC",
            "price_desc": "products.price DESC",
            "name_asc": "products.name ASC",
            "newest": "products.created_at DESC",
        }[sort]

        if q.strip() and sort == "relevance":
            base = f"""
                SELECT products.*, products_fts.rank AS rank
                FROM products JOIN products_fts ON products.id = products_fts.rowid
                {where_clause}
                ORDER BY {order}
            """
        else:
            base = f"SELECT * FROM products {where_clause} ORDER BY {order}"

        total = conn.execute(
            f"SELECT COUNT(*) FROM products {where_clause}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"{base} LIMIT ? OFFSET ?", [*params, page_size, (page - 1) * page_size]
        ).fetchall()

        results = []
        for r in rows:
            d = dict(r)
            d.pop("rank", None)
            results.append(d)

        attach_restock_flags(conn, results)

        return JSONResponse({
            "results": results,
            "total": total,
            "page": page,
            "page_size": page_size,
        })
    finally:
        conn.close()


app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
