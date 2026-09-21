"""Search API: FTS query building, filters, sort, detail, restock flags.

Hits the FastAPI app against a tiny fixture database. Nothing here touches
the live products.db or the network.
"""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as search_app  # noqa: E402
from app import fts_match_query  # noqa: E402


# ---- fts_match_query --------------------------------------------------------

@pytest.mark.parametrize("q, expected", [
    ("", None),
    ("   ", None),
    ('"""', None),
    ("***", None),
    ("wyld", '"wyld"*'),
    ("wyld gummies", '"wyld"* AND "gummies"*'),
    ('wyld "gummies', '"wyld"* AND "gummies"*'),     # a typed quote used to 500
    ("Nate's", '"Nate\'s"*'),
    (".5g cart", '".5g"* AND "cart"*'),
])
def test_fts_match_query(q, expected):
    assert fts_match_query(q) == expected


# ---- fixture db + client ----------------------------------------------------

def _seed(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE products (
        id INTEGER PRIMARY KEY, product_id TEXT, name TEXT, brand_name TEXT,
        dispensary_display TEXT, dispensary_slug TEXT, product_type TEXT,
        weight_label TEXT, price REAL, sale_price REAL, created_at TEXT,
        image_url TEXT, product_url TEXT)""")
    rows = [
        (1, "p1", "Wyld Gummies Huckleberry", "Wyld", "Jars 16th", "jars-16th",
         "Edible", "10pk", 18.0, 14.0, "2026-09-18 10:00:00", "", "http://a"),
        (2, "p2", "Blue Dream Flower", "Native", "Native Roots", "native-roots",
         "Flower", "3.5g", 25.0, None, "2026-09-17 10:00:00", "", "http://b"),
        (3, "p3", 'Quote "test" Cartridge', "PAX", "Jars 16th", "jars-16th",
         "Vaporizers", "1g", 10.0, None, "2026-09-16 10:00:00", "", "http://c"),
    ]
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("""CREATE VIRTUAL TABLE products_fts USING fts5(
                        name, brand_name, dispensary_display, content='products', content_rowid='id')""")
    conn.execute("""INSERT INTO products_fts(rowid, name, brand_name, dispensary_display)
                    SELECT id, name, brand_name, dispensary_display FROM products""")
    conn.execute("CREATE TABLE import_meta (source_csv TEXT, row_count INTEGER, imported_at TEXT)")
    conn.execute("INSERT INTO import_meta VALUES ('x.csv', 3, datetime('now'))")
    conn.commit(); conn.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "products.db"
    _seed(db)
    monkeypatch.setattr(search_app, "DB_PATH", db)
    from fastapi.testclient import TestClient
    with TestClient(search_app.app) as c:
        yield c, db


def _names(body):
    return [r["name"] for r in body["results"]]


def test_search_and_of_tokens(client):
    c, _ = client
    body = c.get("/api/search", params={"q": "wyld gummies"}).json()
    assert _names(body) == ["Wyld Gummies Huckleberry"]
    assert body["total"] == 1


def test_search_quote_in_query_does_not_500(client):
    c, _ = client
    r = c.get("/api/search", params={"q": 'quote "test'})
    assert r.status_code == 200
    assert _names(r.json()) == ['Quote "test" Cartridge']


def test_search_punctuation_only_is_an_empty_query(client):
    c, _ = client
    body = c.get("/api/search", params={"q": "***"}).json()
    assert body["total"] == 3          # treated as no search, not a MATCH error


def test_search_price_sort_uses_sale_price(client):
    c, _ = client
    body = c.get("/api/search", params={"sort": "price_asc"}).json()
    # 3 at $10, 1 at sale $14 (regular $18), 2 at $25
    assert _names(body) == [
        'Quote "test" Cartridge', "Wyld Gummies Huckleberry", "Blue Dream Flower",
    ]


def test_search_filter_and_restock_flag(client):
    c, db = client
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE restock_events (
        product_id, dispensary_slug, reason, prev_quantity, quantity,
        name, price, dispensary_display)""")
    conn.execute("INSERT INTO restock_events VALUES ('p1','jars-16th','returned',0,5,'x',1,'Jars 16th')")
    conn.commit(); conn.close()
    body = c.get("/api/search", params=[("product_type", "Edible"), ("product_type", "Flower")]).json()
    assert set(_names(body)) == {"Wyld Gummies Huckleberry", "Blue Dream Flower"}
    flagged = next(r for r in body["results"] if r["product_id"] == "p1")
    assert flagged["restock_reason"] == "returned"


def test_filters_narrow_with_query(client):
    c, _ = client
    body = c.get("/api/filters", params={"q": "wyld"}).json()
    assert body["brands"] == ["Wyld"]
    assert "Native" not in body["brands"]


def test_detail_404(client):
    c, _ = client
    r = c.get("/api/products/999/detail")
    assert r.status_code == 404
    assert r.json() == {"error": "not found"}


def test_detail_without_similarity_tables(client):
    c, _ = client
    body = c.get("/api/products/1/detail").json()
    assert body["product"]["name"] == "Wyld Gummies Huckleberry"
    assert body["offers"] == [] and body["similar"] == [] and body["group"] is None


def test_meta(client):
    c, _ = client
    body = c.get("/api/meta").json()
    assert body["row_count"] == 3
