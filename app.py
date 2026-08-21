"""Search API + static frontend for scraped Dutchie product data.

Run with:
    ./.venv/bin/uvicorn app:app --reload --port 8000
Then open http://127.0.0.1:8000
"""
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
        price_row = conn.execute("SELECT MIN(price), MAX(price) FROM products").fetchone()
        return {
            "types": types,
            "dispensaries": dispensaries,
            "min_price": price_row[0],
            "max_price": price_row[1],
        }
    finally:
        conn.close()


@app.get("/api/search")
def search(
    q: str = Query("", description="Free-text search over name/brand/dispensary"),
    product_type: Optional[str] = None,
    dispensary: Optional[str] = None,
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

        if q.strip():
            where.append("id IN (SELECT rowid FROM products_fts WHERE products_fts MATCH ?)")
            params.append(f'"{q.strip()}"*')

        if product_type:
            where.append("product_type = ?")
            params.append(product_type)
        if dispensary:
            where.append("dispensary_display = ?")
            params.append(dispensary)
        if min_price is not None:
            where.append("price >= ?")
            params.append(min_price)
        if max_price is not None:
            where.append("price <= ?")
            params.append(max_price)

        where_clause = f"WHERE {' AND '.join(where)}" if where else ""

        order = {
            "relevance": "id" if not q.strip() else "rank",
            "price_asc": "price ASC",
            "price_desc": "price DESC",
            "name_asc": "name ASC",
            "newest": "created_at DESC",
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

        return JSONResponse({
            "results": results,
            "total": total,
            "page": page,
            "page_size": page_size,
        })
    finally:
        conn.close()


app.mount("/", StaticFiles(directory=PROJECT_ROOT / "static", html=True), name="static")
