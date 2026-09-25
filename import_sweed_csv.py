"""Sweed scraper CSV -> product rows for the search database.

Reads the CSV that dutchie_scraper's ``scrape_sweed_dispensaries.py`` writes
and maps Sweed's records onto the ``products`` columns ``app.py`` serves.
``import_products.py`` combines the result with the other sources; this
module has no entry point of its own.

A store on Sweed runs Sweed as its point of sale, so its Sweed menu is its
live inventory. The CSV's ``supersedesDutchie`` column names the Dutchie
menus the same store keeps (space-separated slugs); import_products.py
moves those to ``unlisted_products`` instead of showing them.

Mapping decisions:

* One row per product **size**: Sweed lists each size as its own entry, so
  ``product_id`` is the variant id, which is unique and stable.
* ``category`` -> Dutchie's ``product_type``; Sweed's ``productType`` within a
  category -> Dutchie's ``product_subcategory`` (see ``SUBCATEGORY``).
* ``sale_price`` is ``promoPrice`` when it undercuts ``price``.
* ``weight_label`` is the size name, spelled the way Dutchie and Jane spell
  it ("1g", ".5g" not "0.5g", "100mg"); "Each" is blank.
  ``weight_mg`` only for gram sizes -- "100mg" on an edible is its THC dose.
* No creation dates: ``created_at`` is filled in by import_products.py with
  the date we first saw the product.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# Sweed category -> Dutchie product_type
PRODUCT_TYPE = {
    "Flower": "Flower", "Cartridges": "Vaporizers", "Vaporizers": "Vaporizers", "Concentrate": "Concentrate",
    "Concentrates": "Concentrate", "Pre-Roll": "Pre-Rolls", "Pre-Rolls": "Pre-Rolls", "Edibles": "Edible",
    "Edible": "Edible", "Paraphernalia": "Accessories", "Accessories": "Accessories", "Tinctures": "Tincture",
    "Tincture": "Tincture", "Topicals": "Topicals", "Topical": "Topicals", "Seeds": "Seeds", "Apparel": "Apparel",
}

# (Sweed category, Sweed productType) -> Dutchie product_subcategory. "" is
# Dutchie's own "no subcategory". Anything not listed falls back to a slug of
# the product type, unless it just repeats the category.
SUBCATEGORY = {
    ("Cartridges", "Cartridges"): "cartridges", ("Cartridges", "Live Resin"): "live-resin-cartridge",
    ("Cartridges", "Live Resin Cart"): "live-resin-cartridge", ("Cartridges", "Live Rosin"): "live-rosin-cartridge",
    ("Cartridges", "Resin Aio"): "all-in-one", ("Cartridges", "Rosin Aio"): "all-in-one",
    ("Concentrate", "Badder"): "badder", ("Concentrate", "Concentrate"): "", ("Concentrate", "Live Resin"): "live-resin",
    ("Concentrate", "Live Resin Badder"): "live-resin", ("Concentrate", "Live Rosin"): "live-rosin",
    ("Concentrate", "Wax"): "wax",
    ("Edibles", "Edible (Liquid)"): "drinks", ("Edibles", "Edible (Solid)"): "",
    ("Flower", "Bulk Flower"): "bulk-flower", ("Flower", "Flower"): "",
    ("Pre-Roll", "Infused"): "infused", ("Pre-Roll", "Infused Joint"): "infused", ("Pre-Roll", "Joint"): "singles",
    ("Pre-Roll", "Pre-Roll"): "singles", ("Pre-Roll", "Resin Rolls"): "infused", ("Pre-Roll", "Rosin Roll"): "infused",
    ("Pre-Roll", "Rosin Rolls"): "infused",
    ("Paraphernalia", "Paraphernalia"): "",
}

STRAIN = {"hybrid": "Hybrid", "indica": "Indica", "sativa": "Sativa", "indica dominant": "Indica-Hybrid",
          "sativa dominant": "Sativa-Hybrid", "cbd": "High CBD"}


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def subcategory(category, product_type) -> str:
    if not isinstance(product_type, str) or not product_type:
        return ""
    if (category, product_type) in SUBCATEGORY:
        return SUBCATEGORY[(category, product_type)]
    return "" if slugify(product_type) == slugify(category) else slugify(product_type)


def weight_label(size) -> str:
    if not isinstance(size, str) or size.strip().lower() == "each":
        return ""
    label = re.sub(r"(?i)(\d)\s*G\b", r"\1g", size.strip())      # "1G" -> "1g"
    return re.sub(r"^0(\.\d)", r"\1", label)                         # "0.5g" -> ".5g", as Dutchie/Jane write it


def weight_mg(value, unit):
    if pd.notna(value) and isinstance(unit, str) and unit.upper() == "G" and float(value) > 0:
        return float(value) * 1000
    return None


def thc_display(value, unit) -> str:
    if pd.isna(value):
        return ""
    return f"{float(value):g}{'%' if unit == '%' else 'mg'}"


def sweed_products(csv_path: Path) -> tuple[pd.DataFrame, dict[str, str]]:
    """The product rows, and {Dutchie slug: Sweed store name} for the Dutchie
    menus each Sweed store supersedes."""
    df = pd.read_csv(csv_path, low_memory=False)
    print(f"{len(df):,} rows in {csv_path.name}")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df = df[df["name"].notna() & df["price"].notna()].copy()
    print(f"{len(df):,} rows have a name and a price")

    promo = pd.to_numeric(df["promoPrice"], errors="coerce")
    out = pd.DataFrame({
        "product_id": df["variantId"].astype("Int64").astype("string"),
        "name": df["name"],
        "image_url": df["image"].fillna(""),
        "price": df["price"],
        "sale_price": promo.where(promo < df["price"]),
        "brand_name": df["brand"].fillna(""),
        "product_type": df["category"].map(PRODUCT_TYPE).fillna(df["category"]).fillna("Uncategorized"),
        "product_subcategory": [subcategory(c, t) for c, t in zip(df["category"], df["productType"])],
        "strain_type": df["strainType"].str.lower().map(STRAIN).fillna(""),
        "weight_label": df["variantName"].map(weight_label),
        "weight_mg": [weight_mg(v, u) for v, u in zip(df["unitSizeValue"], df["unitSizeUnit"])],
        "thc_display": [thc_display(v, u) for v, u in zip(df["thc"], df["thcUnit"])],
        "dispensary_display": df["storeName"],
        "dispensary_slug": df["storeName"].map(slugify),
        "dispensary_url": df["url"],
        "product_url": df["productUrl"],
        "product_slug": df["canonicalName"].fillna(""),
        "scrape_date": df["scrapeDate"],
        "created_at": pd.NaT,
        "updated_at": pd.NaT,
        "quantity_available": pd.to_numeric(df["availableQty"], errors="coerce").round().astype("Int64"),
        "package_id": pd.Series([None] * len(df), dtype="string", index=df.index),
    })

    supersedes = {}
    if "supersedesDutchie" in df:
        for store, slugs in df.groupby("storeName")["supersedesDutchie"].first().items():
            for slug in str(slugs).split() if isinstance(slugs, str) else []:
                supersedes[slug] = store
    return out.reset_index(drop=True), supersedes
