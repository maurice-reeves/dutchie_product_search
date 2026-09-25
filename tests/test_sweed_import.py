"""Sweed stores: the CSV mapping, and the Dutchie menus a Sweed store
supersedes moving to unlisted_products instead of the listing."""
import sqlite3
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import import_products as ip  # noqa: E402
import import_sweed_csv as sw  # noqa: E402

COLUMNS = ["productId", "variantId", "name", "brand", "category", "subcategory", "productType", "strain",
           "strainType", "variantName", "sku", "unitSizeValue", "unitSizeUnit", "price", "promoPrice",
           "availableQty", "saleType", "thc", "thcUnit", "cbd", "cbdUnit", "image", "productUrl",
           "canonicalName", "storeId", "storeName", "storeAddress", "storeCity", "storeState",
           "storeLatitude", "storeLongitude", "url", "platform", "scrapeDate", "supersedesDutchie"]
SHOP = "https://shop.krystaleaves.com/menu"


def sweed_csv(tmp_path, rows):
    base = dict.fromkeys(COLUMNS)
    base.update(storeId=984, storeName="Krystaleaves - Denver", url=SHOP, platform="sweed",
                scrapeDate="2026-09-25", supersedesDutchie="krystaleaves-retail krystaleaves1")
    path = tmp_path / "sweed_products20260925_122313.csv"
    pd.DataFrame([{**base, **r} for r in rows], columns=COLUMNS).to_csv(path, index=False)
    return path


ROWS = [
    dict(productId=1, variantId=11, name="Sweet Lipz", brand="Soiku Bano", category="Cartridges",
         productType="Rosin Aio", variantName="0.5g", unitSizeValue=0.5, unitSizeUnit="G", price=37.0,
         thc=74.4, thcUnit="%", availableQty=8, strainType="Hybrid", productUrl=f"{SHOP}/menu/x-11",
         canonicalName="sweet-lipz", image="https://img/11.webp"),
    dict(productId=2, variantId=21, name="Pineapple Orange Rozzies", brand="Rebel Edibles", category="Edibles",
         productType="Edible (Solid)", variantName="100mg", unitSizeValue=100, unitSizeUnit="MG", price=14.99,
         promoPrice=12.0, thc=100, thcUnit="MG", availableQty=3.0),
    dict(productId=3, variantId=31, name="Cap Junky", brand="Viola", category="Flower", productType="Bulk Flower",
         variantName="3G", unitSizeValue=3, unitSizeUnit="G", price=70.0, strainType="Indica Dominant"),
    dict(productId=4, variantId=41, name="Tips", category="Paraphernalia", productType="Paraphernalia",
         variantName="Each", price=1.0, promoPrice=2.0),
    dict(productId=5, variantId=51, name=None, category="Flower", price=10.0),          # no name: dropped
]


def test_mapping_onto_the_products_columns(tmp_path):
    out, supersedes = sw.sweed_products(sweed_csv(tmp_path, ROWS))
    assert len(out) == 4
    lipz, rozzies, cap, tips = (out.iloc[i] for i in range(4))
    assert (lipz.product_id, lipz.product_type, lipz.product_subcategory) == ("11", "Vaporizers", "all-in-one")
    assert (lipz.weight_label, lipz.weight_mg, lipz.thc_display) == (".5g", 500.0, "74.4%")   # Dutchie/Jane spelling
    assert (lipz.dispensary_display, lipz.dispensary_slug) == ("Krystaleaves - Denver", "krystaleaves-denver")
    assert lipz.product_url == f"{SHOP}/menu/x-11" and lipz.quantity_available == 8
    assert (rozzies.product_type, rozzies.thc_display, rozzies.sale_price) == ("Edible", "100mg", 12.0)
    assert pd.isna(rozzies.weight_mg)                     # "100mg" is a dose, not a weight
    assert (cap.weight_label, cap.product_subcategory, cap.strain_type) == ("3g", "bulk-flower", "Indica-Hybrid")
    assert (tips.product_type, tips.weight_label) == ("Accessories", "")
    assert pd.isna(tips.sale_price)                       # a "promo" above the price is no sale
    assert supersedes == {"krystaleaves-retail": "Krystaleaves - Denver", "krystaleaves1": "Krystaleaves - Denver"}


def dutchie_rows():
    return pd.DataFrame({
        "product_id": ["a", "b", "c"], "name": ["X", "Y", "Z"],
        "dispensary_display": ["Krystaleaves Retail", "Krystaleaves1", "Native Roots"],
        "dispensary_slug": ["krystaleaves-retail", "krystaleaves1", "native-roots"],
        "created_at": ["2026-09-01", "2026-09-01", "2026-09-01"], "updated_at": [None, None, None],
    })


def test_superseded_dutchie_menus_are_moved_not_dropped(tmp_path):
    sweed, supersedes = sw.sweed_products(sweed_csv(tmp_path, ROWS))
    listed, unlisted, lines = ip.apply_sweed(dutchie_rows(), sweed, supersedes)
    assert set(listed.dispensary_display) == {"Native Roots", "Krystaleaves - Denver"}
    assert sorted(unlisted.dispensary_slug) == ["krystaleaves-retail", "krystaleaves1"]
    assert set(unlisted.unlisted_reason) == {"superseded by Sweed: Krystaleaves - Denver"}
    assert len(lines) == 2 and "not listed" in lines[0]


def test_without_sweed_nothing_is_hidden():
    listed, unlisted, lines = ip.apply_sweed(dutchie_rows(), None, {})
    assert len(listed) == 3 and unlisted.empty and lines == []


def test_unlisted_rows_land_in_their_own_table(tmp_path, monkeypatch):
    monkeypatch.setattr(ip, "DB_PATH", tmp_path / "products.db")
    sweed, supersedes = sw.sweed_products(sweed_csv(tmp_path, ROWS))
    listed, unlisted, _ = ip.apply_sweed(dutchie_rows().assign(scrape_date="2026-09-25", price=1.0,
                                                               brand_name="", product_type="Flower"),
                                         sweed, supersedes)
    ip.write_database(listed, [Path("d.csv")], unlisted=unlisted)
    conn = sqlite3.connect(tmp_path / "products.db")
    shown = {r[0] for r in conn.execute("SELECT dispensary_slug FROM products")}
    hidden = {r[0] for r in conn.execute("SELECT dispensary_slug FROM unlisted_products")}
    searchable = conn.execute("SELECT COUNT(*) FROM products_fts WHERE products_fts MATCH 'Krystaleaves1'").fetchone()[0]
    conn.close()
    assert "krystaleaves-retail" not in shown and "krystaleaves-denver" in shown
    assert hidden == {"krystaleaves-retail", "krystaleaves1"}
    assert searchable == 0


def test_a_stale_sweed_csv_is_ignored(tmp_path):
    dutchie = tmp_path / "all_dispensaries20260925_063235.csv"
    assert ip.choose_sweed(tmp_path / "sweed_products20260925_122313.csv", dutchie) is not None
    assert ip.choose_sweed(tmp_path / "sweed_products20260910_122313.csv", dutchie) is None
    assert ip.choose_sweed(None, dutchie) is None
