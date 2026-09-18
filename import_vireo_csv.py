"""Vireo (Jane) scraper CSV -> product rows for the search database.

Reads the CSV that dutchie_scraper's ``scrape_vireo_dispensaries.py`` writes
and maps Jane's record shape onto the ``products`` columns ``app.py`` serves.
``import_products.py`` combines the result with the Dutchie rows and writes
the database; this module has no entry point of its own.

Mapping decisions (see ``PRODUCT_TYPE`` / ``SUBCATEGORY`` below for the vocabularies):

* Rows are **not** dropped for a missing image; only a missing name or price.
* ``price`` is Jane's ``bucket_price`` -- the "from" price shown on the menu card.
* ``kind`` and the subtype are normalised to Dutchie's ``product_type`` /
  ``product_subcategory`` vocabularies so the filters merge cleanly.
* THC is ``percent_thc`` where Jane has it (flower, vape, extract, pre-roll),
  else the ``dosage`` string (edibles: "100mg", "100mg CBD/100mg THC").
* The weight label comes from ``available_weights`` (Dutchie-style labels:
  1g, 1/8oz, ...), falling back to Jane's ``amount`` ("10pk", "1000mg").
* No creation/update dates or package ids exist on Jane. ``created_at`` is
  filled in by import_products.py with the date we first saw the product.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pandas as pd

# Jane `kind` -> Dutchie `product_type`
PRODUCT_TYPE = {
    "flower": "Flower", "vape": "Vaporizers", "edible": "Edible", "extract": "Concentrate",
    "pre-roll": "Pre-Rolls", "tincture": "Tincture", "topical": "Topicals", "gear": "Accessories",
    "grow": "Seeds", "merch": "Apparel",
}

# (Jane kind, Jane root_subtype) -> Dutchie product_subcategory. "" is Dutchie's
# own "no subcategory". Anything not listed falls back to a slug of the Jane
# subtype so it still shows up as a filter value.
SUBCATEGORY = {
    ("vape", "Disposables"): "disposables", ("vape", "Cartridges"): "cartridges",
    ("vape", "Specialty Pods"): "pods", ("vape", "All-In-One"): "all-in-one",
    ("edible", "Candies"): "gummies", ("edible", "Drinks"): "drinks", ("edible", "Chocolates"): "chocolates",
    ("edible", "Baked Goods"): "baked-goods", ("edible", "Drink Mixes"): "drink-mixes",
    ("edible", "Tablets"): "capsules-tablets", ("edible", "Capsules"): "capsules-tablets",
    ("edible", "Lozenges"): "lozenges", ("edible", "Mints"): "mints",
    ("flower", "Flower"): "", ("flower", "Pre Pack"): "whole-flower", ("flower", "Shake"): "shake-trim",
    ("flower", "Infused Flower"): "infused-bud", ("flower", "Premium"): "premium",
    ("flower", "Ground Flower"): "pre-ground", ("flower", "Select"): "", ("flower", "Silver"): "",
    ("flower", "Gold"): "", ("flower", "Smalls"): "small-buds", ("flower", "Popcorn"): "smalls-popcorn",
    ("extract", "Waxes"): "wax", ("extract", "Rosins"): "rosin", ("extract", "Live Resins"): "live-resin",
    ("extract", "Hash"): "hash", ("extract", "Rick Simpson Oil (RSO)"): "rso", ("extract", "Shatters"): "shatter",
    ("extract", "Sauces"): "sauce", ("extract", "Budders"): "budder", ("extract", "Badders"): "badder",
    ("extract", "Sugars"): "sugar", ("extract", "Diamonds"): "diamonds", ("extract", "Distillates"): "distillate",
    ("extract", "Kief"): "kief", ("extract", "Crumbles"): "crumble", ("extract", "Applicators"): "applicators",
    ("pre-roll", "Infused"): "infused", ("pre-roll", "Pre Roll Packs"): "packs", ("pre-roll", "Pre Rolls"): "singles",
    ("pre-roll", "Infused Packs"): "infused-pre-roll-packs", ("pre-roll", "Infused Blunts"): "blunts",
    ("pre-roll", "Blunts"): "blunts",
    ("gear", "Vaporizers"): "batteries", ("gear", "Paraphernalia"): "", ("gear", "Accessories"): "",
    ("gear", "Papers"): "papers-rolling-supplies", ("gear", "Cones"): "papers-rolling-supplies",
    ("gear", "Wraps"): "papers-rolling-supplies", ("gear", "Tips"): "papers-rolling-supplies",
    ("gear", "Grinders"): "grinders", ("gear", "Glass"): "glassware", ("gear", "Lighters"): "lighters",
    ("gear", "Trays"): "trays", ("gear", "Storage"): "storage-containers", ("gear", "Clothing"): "",
    ("topical", "Creams"): "lotions", ("topical", "Lotions"): "lotions", ("topical", "Patches"): "transdermal-patches",
    ("topical", "Balms"): "balms", ("topical", "Bath"): "bath-products", ("topical", "Oils"): "oils",
    ("tincture", "Sublinguals"): "", ("tincture", "Tinctures"): "",
    ("vape", "Vape Kits"): "vaporizer-bundles", ("flower", "Value"): "", ("flower", "Bronze"): "",
    ("extract", "Syringes/Tankers"): "applicators", ("extract", "Moonrocks"): "infused-bud",
    ("topical", "Salves"): "balms", ("pre-roll", "Blunt Packs"): "blunts", ("grow", "Seeds"): "",
    ("edible", "Cooking"): "cooking-baking", ("edible", "Bars"): "chocolates",
}
# Jane's catch-all buckets carry no information worth a filter value.
GENERIC_SUBTYPES = {"Other", "Clothing", "Seeds"}
# Edible "Candies" is broad on Jane; the brand subtype splits it the way Dutchie does.
EDIBLE_BRAND_SUBTYPE = {"Gummies": "gummies", "Chocolates": "chocolates", "Beverages": "drinks",
                        "Desserts - Baked Goods": "baked-goods", "Tablets": "capsules-tablets",
                        "Confections": "candy", "Fruit Chews": "chews", "Soda": "drinks"}

STRAIN = {"hybrid": "Hybrid", "indica": "Indica", "sativa": "Sativa", "cbd": "High CBD"}

# Jane weight keys, in the order Dutchie shoppers expect them, -> Dutchie labels
WEIGHT_LABEL = {"half gram": ".5g", "gram": "1g", "two gram": "2g", "eighth ounce": "1/8oz",
                "quarter ounce": "1/4oz", "half ounce": "1/2oz", "ounce": "1oz"}


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


def as_list(value):
    """CSV cells that were Python lists come back as their repr."""
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return []
    return []


def subcategory(kind, root_subtype, brand_subtype) -> str:
    if kind == "edible" and brand_subtype in EDIBLE_BRAND_SUBTYPE and root_subtype in ("Candies", None, ""):
        return EDIBLE_BRAND_SUBTYPE[brand_subtype]
    if root_subtype in GENERIC_SUBTYPES:
        return ""
    if (kind, root_subtype) in SUBCATEGORY:
        return SUBCATEGORY[(kind, root_subtype)]
    return slugify(root_subtype) if isinstance(root_subtype, str) and root_subtype else ""


def product_type(kind, root_subtype, name) -> str:
    if kind == "gear" and root_subtype == "Clothing":
        return "Apparel"
    if kind == "grow" and isinstance(name, str) and "clone" in name.lower():
        return "Clones"
    return PRODUCT_TYPE.get(kind, "Uncategorized")


# Kinds whose potency is a dose in mg, not a percentage. Jane sometimes stores
# the mg figure in percent_thc for these (an edible showing "100%"), so the
# dosage string wins.
DOSED_KINDS = {"edible", "tincture", "topical"}


def thc_display(kind, percent, dosage) -> str:
    dose = dosage.strip().rstrip(")") if isinstance(dosage, str) else ""
    if kind in DOSED_KINDS:
        if dose:
            return dose
        return f"{float(percent):g}mg" if pd.notna(percent) else ""
    if pd.notna(percent):
        return f"{float(percent):g}%"
    return dose


def weight_label(available, amount) -> str:
    weights = [w for w in as_list(available) if w in WEIGHT_LABEL]
    if weights:
        return WEIGHT_LABEL[min(weights, key=list(WEIGHT_LABEL).index)]
    return amount if isinstance(amount, str) else ""


def first_image(value) -> str:
    urls = as_list(value)
    return urls[0] if urls else ""


def vireo_products(csv_path: Path) -> pd.DataFrame:
    df = pd.read_csv(csv_path, index_col=0, low_memory=False)
    print(f"{len(df):,} rows in {csv_path.name}")

    df["price"] = pd.to_numeric(df["bucket_price"], errors="coerce")
    df = df[df["name"].notna() & df["price"].notna()].copy()
    print(f"{len(df):,} rows have a name and a price (images are optional)")

    store_slug = df["storeName"].map(slugify)
    out = pd.DataFrame({
        "product_id": df["product_id"].astype("Int64").astype("string"),
        "name": df["name"],
        "image_url": df["image_urls"].map(first_image),
        "price": df["price"],
        "brand_name": df["brand"].fillna(""),
        "product_type": [product_type(k, s, n) for k, s, n in zip(df["kind"], df["root_subtype"], df["name"])],
        "product_subcategory": [subcategory(k, s, b) for k, s, b in zip(df["kind"], df["root_subtype"], df.get("brand_subtype"))],
        "strain_type": df["category"].map(STRAIN).fillna(""),
        "weight_label": [weight_label(a, m) for a, m in zip(df["available_weights"], df["amount"])],
        "weight_mg": pd.to_numeric(df["net_weight_grams"], errors="coerce") * 1000,
        "thc_display": [thc_display(k, p, d) for k, p, d in zip(df["kind"], df["percent_thc"], df.get("dosage"))],
        "dispensary_display": df["storeName"],
        "dispensary_slug": store_slug,
        "dispensary_url": df["url"],
        "product_url": [f"https://www.{site}/shop/products/{pid}/{slug}" for site, pid, slug in zip(df["brandSite"], df["product_id"], df["url_slug"].fillna(""))],
        "product_slug": df["url_slug"].fillna(""),
        "scrape_date": df["scrapeDate"],
        "created_at": pd.NaT,
        "updated_at": pd.NaT,
        "quantity_available": pd.to_numeric(df["max_cart_quantity"], errors="coerce").astype("Int64"),
        "package_id": pd.Series([None] * len(df), dtype="string", index=df.index),
    })
    out.loc[out["weight_mg"] <= 0, "weight_mg"] = None
    return out.reset_index(drop=True)
