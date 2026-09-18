"""Search API + static frontend for scraped Dutchie product data.

Run with:
    ./.venv/bin/uvicorn app:app --reload --port 8000
Then open http://127.0.0.1:8000
"""
import os
import re
import sqlite3
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import dashboard

PROJECT_ROOT = Path(__file__).resolve().parent
# PRODUCTS_DB lets a dev instance serve a different database (e.g. one that
# includes the Vireo/Jane import) on another port without touching production.
DB_PATH = Path(os.environ.get("PRODUCTS_DB", PROJECT_ROOT / "data" / "products.db"))

# The lifespan runs the dashboard's metrics sampler alongside the app.
app = FastAPI(title="Dutchie Product Search", lifespan=dashboard.lifespan)


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


# Leading-dot labels are common in this data (".1g", ".5g"), so the integer
# part has to be optional — requiring a digit first sent them to the
# non-weight bucket and they sorted after 100g.
_WEIGHT_PATTERN = re.compile(r"^\s*(\d*\.\d+|\d+\.?\d*|\d+/\d+)\s*(g|mg|oz|lb)\s*$", re.I)
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


# name in the response -> column on `products`
FACET_COLUMNS = {
    "types": "product_type",
    "dispensaries": "dispensary_display",
    "brands": "brand_name",
    "weights": "weight_label",
}


def facet_conditions(selections: dict, exclude: str = None, q: str = ""):
    """WHERE fragments for every selected facet except `exclude`.

    A facet is excluded from its own option list so the list keeps showing the
    alternatives you could switch to. Narrowing "brands" by the brands already
    picked would collapse it to exactly what is selected, leaving no way to add
    a second brand.
    """
    where, params = [], []
    if q.strip():
        where.append("id IN (SELECT rowid FROM products_fts WHERE products_fts MATCH ?)")
        params.append(f'"{q.strip()}"*')
    for name, column in FACET_COLUMNS.items():
        if name == exclude:
            continue
        chosen = [v for v in (selections.get(name) or []) if v]
        if chosen:
            where.append(f"{column} IN ({','.join('?' * len(chosen))})")
            params.extend(chosen)
    return where, params


@app.get("/api/filters")
def filters(
    q: str = Query(""),
    product_type: Optional[List[str]] = Query(None),
    dispensary: Optional[List[str]] = Query(None),
    brand: Optional[List[str]] = Query(None),
    weight: Optional[List[str]] = Query(None),
):
    """Available options for each filter, narrowed by the other filters.

    Pass the current selections and each list comes back containing only values
    that still have matching products, so picking a brand shrinks the weight
    list to that brand's sizes.
    """
    selections = {
        "types": product_type, "dispensaries": dispensary,
        "brands": brand, "weights": weight,
    }
    conn = get_conn()
    try:
        out = {}
        for name, column in FACET_COLUMNS.items():
            where, params = facet_conditions(selections, exclude=name, q=q)
            clause = f"WHERE {' AND '.join(where)}" if where else ""
            sql = (
                f"SELECT DISTINCT {column} FROM products {clause}"
                if clause else f"SELECT DISTINCT {column} FROM products"
            )
            values = [r[0] for r in conn.execute(sql, params) if r[0]]
            out[name] = (
                sorted(values, key=weight_sort_key) if name == "weights"
                else sorted(values, key=str.lower)
            )
        price_where, price_params = facet_conditions(selections, q=q)
        clause = f"WHERE {' AND '.join(price_where)}" if price_where else ""
        price_row = conn.execute(
            f"SELECT MIN(price), MAX(price) FROM products {clause}", price_params
        ).fetchone()
        out["min_price"], out["max_price"] = price_row[0], price_row[1]
        return out
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
    product_type: Optional[List[str]] = Query(None),
    dispensary: Optional[List[str]] = Query(None),
    brand: Optional[List[str]] = Query(None),
    weight: Optional[List[str]] = Query(None),
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

        # Each filter is multi-select: repeated query params become a list and
        # widen the match rather than narrowing it. Different filters still AND
        # together, so "Flower or Edible" at "Wyld or PAX" behaves as expected.
        for column, values in (
            ("products.product_type", product_type),
            ("products.dispensary_display", dispensary),
            ("products.brand_name", brand),
            ("products.weight_label", weight),
        ):
            chosen = [v for v in (values or []) if v]
            if chosen:
                where.append(f"{column} IN ({','.join('?' * len(chosen))})")
                params.extend(chosen)
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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


@app.get("/api/products/{product_row}/detail")
def product_detail(product_row: int):
    """Everything the product popup needs in one round-trip.

    `offers` are the same product at other dispensaries -- one row per store,
    that store's lowest price *at the same size* as the product being viewed,
    each carrying the confidence of the match so the page can flag the shaky
    ones and let the shopper untick them. `similar` are the nearest
    embedding neighbours outside the product's own group; they share the
    offers' table on the page (unticked until the shopper opts them in), so
    each also carries its price at the size being compared when it is sold
    in that size (`price_at_size`), else only its listed price and size.
    Both come from tables that build_similarity.py writes; on a database
    without them they are simply empty and the popup shows the product alone.
    """
    conn = get_conn()
    try:
        product = conn.execute("SELECT * FROM products WHERE id = ?", (product_row,)).fetchone()
        if product is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        product = dict(product)
        out = {"product": product, "sizes": [], "group": None, "offers": [], "similar": []}
        if not _table_exists(conn, "product_groups"):
            return JSONResponse(out)

        out["sizes"] = [dict(r) for r in conn.execute(
            "SELECT size_key, size_label, price, sale_price FROM product_prices WHERE product_row = ? ORDER BY price", (product_row,))]
        # The size the card's price refers to: the row whose label matches the
        # product's weight label, else the first (cheapest) size.
        primary = next((s for s in out["sizes"] if s["size_label"] == product.get("weight_label")), out["sizes"][0] if out["sizes"] else None)

        grp = conn.execute("SELECT group_id, confidence, method FROM product_groups WHERE product_row = ?", (product_row,)).fetchone()
        if grp and primary:
            size_key = primary["size_key"]
            rows = conn.execute("""
                SELECT p.id, p.name, p.brand_name, p.weight_label, p.image_url, p.dispensary_display, p.dispensary_slug,
                       p.product_url, p.quantity_available, g.confidence, g.method, pp.price, pp.sale_price, pp.size_label
                FROM product_groups g
                JOIN products p ON p.id = g.product_row
                JOIN product_prices pp ON pp.product_row = p.id AND pp.size_key = ?
                WHERE g.group_id = ?
                ORDER BY pp.price""", (size_key, grp["group_id"])).fetchall()
            offers, seen = [], set()
            for r in rows:                              # first row per store is that store's lowest price
                if r["dispensary_slug"] in seen:
                    continue
                seen.add(r["dispensary_slug"])
                o = dict(r)
                o["is_this"] = r["id"] == product_row
                offers.append(o)
            out["group"] = {"group_id": grp["group_id"], "confidence": grp["confidence"], "method": grp["method"],
                            "size_key": size_key, "size_label": primary["size_label"], "n_stores": len(offers)}
            out["offers"] = offers

        size_key = out["group"]["size_key"] if out["group"] else None
        similar, seen = [], set()
        for r in conn.execute("""
            SELECT p.id, p.name, p.brand_name, p.product_type, p.weight_label, p.thc_display, p.price,
                   p.image_url, p.dispensary_display, p.dispensary_slug, p.product_url, s.score,
                   pp.price AS price_at_size, pp.sale_price AS sale_at_size, pp.size_label AS size_label_at_size
            FROM similar_products s JOIN products p ON p.id = s.similar_row
            LEFT JOIN product_prices pp ON pp.product_row = p.id AND pp.size_key = ?
            WHERE s.product_row = ? ORDER BY s.rank, pp.price""", (size_key, product_row)):
            if r["id"] not in seen:                     # a size can carry two labels; keep the cheaper row
                seen.add(r["id"])
                similar.append(dict(r))
        out["similar"] = similar
        return JSONResponse(out)
    finally:
        conn.close()


# Private owner-only status page (/dash) plus the public visitor heartbeat.
# Registered before the static mount, which would otherwise swallow /dash.
app.include_router(dashboard.router)

app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
