"""Sale-price regression checks using temporary CSVs and SQLite databases."""
import json
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import import_csv


def search(**kwargs):
    args = dict(q="", product_type=None, dispensary=None, brand=None, weight=None,
                min_price=None, max_price=None, sort="price_asc", page=1, page_size=30)
    args.update(kwargs)
    return json.loads(app.search(**args).body)


@pytest.fixture
def price_db(tmp_path, monkeypatch):
    path = tmp_path / 'prices.db'
    monkeypatch.setattr(app, 'DB_PATH', path)
    with sqlite3.connect(path) as conn:
        conn.execute('''CREATE TABLE products (
            id INTEGER PRIMARY KEY, name TEXT, price REAL, sale_price REAL,
            product_type TEXT, dispensary_display TEXT, brand_name TEXT, weight_label TEXT)''')
        conn.executemany('INSERT INTO products VALUES (?, ?, ?, ?, ?, ?, ?, ?)', [
            (1, 'Test sale', 80, 10, 'Flower', 'A', 'Brand', '1g'),
            (2, 'Test normal', 15, None, 'Flower', 'B', 'Brand', '1g'),
            (3, 'Test zero', 20, 0, 'Flower', 'C', 'Brand', '1g'),
            (4, 'Test negative', 25, -1, 'Flower', 'D', 'Brand', '1g'),
            (5, 'Test higher', 30, 40, 'Flower', 'E', 'Brand', '1g'),
        ])
        conn.execute("CREATE VIRTUAL TABLE products_fts USING fts5(name, content='products', content_rowid='id')")
        conn.execute("INSERT INTO products_fts(products_fts) VALUES ('rebuild')")
    return path


def test_sort_and_price_filters_use_positive_discounts(price_db):
    assert [r['id'] for r in search()['results']] == [1, 2, 3, 4, 5]
    assert [r['id'] for r in search(sort='price_desc')['results']] == [5, 4, 3, 2, 1]
    assert [r['id'] for r in search(max_price=12)['results']] == [1]
    assert [r['id'] for r in search(min_price=16, max_price=25)['results']] == [3, 4]
    assert search(q='Test', sort='relevance', max_price=12)['total'] == 1
    facets = app.filters(q='', product_type=None, dispensary=None, brand=None, weight=None)
    assert (facets['min_price'], facets['max_price']) == (10, 30)


def test_price_sort_supports_database_before_sale_column(price_db):
    with sqlite3.connect(price_db) as conn:
        conn.execute('ALTER TABLE products DROP COLUMN sale_price')
    assert [r['id'] for r in search()['results']] == [2, 3, 4, 5, 1]
    assert search(max_price=12)['total'] == 0


@pytest.mark.parametrize('special, expected', [('[0]', None), ('[-1]', None), ('[10]', 10),
                                            ('[20]', None), ('[25]', None), (None, None)])
def test_import_rejects_placeholder_specials(tmp_path, special, expected):
    row = dict.fromkeys(import_csv.USE_COLUMNS, '')
    row.update(Name='Test', Image='https://example.com/image.png', Prices=20, id='1',
               dispensary='test-store', Options="['1g']", recSpecialPrices=special,
               createdAt='2026-09-20', updatedAt='2026-09-20', type='Flower')
    if special is None:
        del row['recSpecialPrices']  # Older exports omit the column entirely.
    path = tmp_path / 'products.csv'
    pd.DataFrame([row]).to_csv(path, index=False)
    actual = import_csv.dutchie_products(path).iloc[0]['sale_price']
    assert pd.isna(actual) if expected is None else actual == expected


def test_import_keeps_one_row_per_listing(tmp_path):
    """A product served on two menu pages arrives twice in the CSV; the
    feed must show it once. Keys on the Dutchie id and the dispensary."""
    row = dict.fromkeys(import_csv.USE_COLUMNS, '')
    row.update(Name='Bernie Hanna Butter', Image='https://example.com/i.png', Prices=5.84, id='6a1e',
               dispensary='golden-meds-lakewood', Options="['1g']", createdAt='2026-06-02', updatedAt='2026-09-21', type='Flower')
    other_store = dict(row, dispensary='golden-meds-superstore')
    path = tmp_path / 'products.csv'
    pd.DataFrame([row, row, other_store]).to_csv(path, index=False)
    out = import_csv.dutchie_products(path)
    assert len(out) == 2 and sorted(out['dispensary_slug']) == ['golden-meds-lakewood', 'golden-meds-superstore']
