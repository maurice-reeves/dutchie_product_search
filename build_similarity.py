#!/usr/bin/env python3
"""Nightly similarity build: per-size prices, same-product groups, similar products.

    /usr/local/bin/python3 build_similarity.py all --db data/products_dev.db

Writes four tables into the products database that the site reads with plain
SQL (see PLAN_similar_products.md for the design and the calibration):

    product_prices   one row per product per size:  (product_row, size_key, size_label, price, sale_price)
    product_groups   which "same product" group each row belongs to, with a confidence and the tier that placed it
    group_prices     per (group, size): number of stores and min/median/avg/max of each store's best price
    similar_products top-12 embedding neighbours per product, excluding rows from its own group

Stages, because `torch` and `faiss` cannot share a process on macOS (two OpenMP
runtimes -> segfault):

    embed    products -> MiniLM embeddings          (torch)      data/<db>.emb.npy + .ids.npy
    index    embeddings -> top-30 neighbours         (faiss)      data/<db>.nnI.npy + .nnD.npy + .faiss
    groups   prices + groups + similar -> DB tables  (numpy only)
    all      runs embed and index as subprocesses, then groups

Runs under the default /usr/local/bin/python3, which has sentence-transformers,
faiss and torch; the web app's venv does not need any of them.

Group prices are per store (a store's lowest listing for that size) and per
size, so a $10 gram and a $25 eighth of the same strain never mix.
"""
from __future__ import annotations

import argparse
import ast
import glob
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
PARENT = PROJECT_ROOT.parent
MODEL = "all-MiniLM-L6-v2"
TOP_K = 60
KEEP_SIMILAR = 12

# ----------------------------------------------------------------------------- normalisation

SIZE_TOKENS = re.compile(r"\b\d+(?:\.\d+)?\s?(?:x\s?\d+(?:\.\d+)?\s?)?(?:mg(?:thc|cbd|cbn|cbg)?|g|oz|ml|pk|pack|ct|piece|pc|count)\b|\b\d+\s?x\s?\d+\b", re.I)
PACK = re.compile(r"\b(\d+)\s?(?:pk|pack|ct|count)\b", re.I)
DOSE = re.compile(r"(\d+(?:\.\d+)?)\s?mg", re.I)
STRAIN = re.compile(r"\b(sativa|indica|hybrid|cbd)\b|\(([sih])\)|-\(([sih])\)-|\b([sih])\b(?=\s*[-|)])", re.I)
LABEL_RE = re.compile(r"^(\d*\.?\d+)\s?(g|mg|oz)$", re.I)
LABEL_MG = {"1/8oz": 3500, "1/4oz": 7000, "1/2oz": 14000, "1oz": 28000, "3.5g": 3500, "7g": 7000, "14g": 14000, "28g": 28000}
NOISE = {"rec", "med", "the", "and", "with"}
# Words that describe the category, format, strain or tier rather than the
# product. They are removed before measuring name overlap in the fuzzy tiers:
# every Lazercat item shares "live rosin aio", so counting those words made
# different strains look alike.
GENERIC = set("""live resin rosin cart carts cartridge cartridges aio all in one disposable disposables pod pods vape vapor vaporizer
wax badder budder sugar shatter hash diamonds diamond sauce rso oil gummies gummy chews chew candy drink drinks soda cola chocolate bar
bars tincture flower prepack prepacked pre roll rolls preroll prerolls joint joints infused blunt blunts pack packs single singles
indica sativa hybrid cbd thc cbn cbg cbc h s i ih sh fs full spectrum solventless distillate cured 90u 120u premium top shelf tier
oz g mg x plus of w papers paper cones cone wraps wrap king size mini classic original organic hemp glass pipe battery kit starter
edible edibles extract concentrate topical cream balm patch capsule tablet dose dosed each ct pk""".split())
BATCH = re.compile(r"#\s?\d+\w*")
# Product format words. A cartridge and an all-in-one of the same strain are
# different SKUs at different prices; so are a starter kit and a disposable.
FORMATS = {"cart": "cart", "carts": "cart", "cartridge": "cart", "cartridges": "cart", "pod": "pod", "pods": "pod",
           "aio": "aio", "disposable": "aio", "disposables": "aio", "kit": "kit", "pack": "pack", "packs": "pack"}


def format_of(name):
    toks = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).split()
    if "all" in toks and "one" in toks:
        return "aio"
    for t in toks:
        if t in FORMATS:
            return FORMATS[t]
    return None
DOSED_TYPES = {"Edible", "Tincture", "Topicals"}
JANE_PRICE_COLS = {"price_half_gram": ".5g", "price_gram": "1g", "price_two_gram": "2g", "price_eighth_ounce": "1/8oz",
                   "price_quarter_ounce": "1/4oz", "price_half_ounce": "1/2oz", "price_ounce": "1oz"}


def brand_norm(b) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(b or "").lower()).replace(" brands", "").replace(" labs", "").strip()


def name_norm(name, brand) -> str:
    """Lowercase, sizes/doses and brand words removed, filler dropped.

    Parenthetical words are kept on purpose: "(Indica)" vs "(Hybrid)", "(Black)"
    vs "(Tan)" and "(M)" vs "(L)" are different products.
    """
    s = BATCH.sub(" ", SIZE_TOKENS.sub(" ", str(name or "").lower()))
    for tok in brand_norm(brand).split():
        if len(tok) > 2:
            s = re.sub(rf"\b{re.escape(tok)}\b", " ", s)
    return " ".join(t for t in re.sub(r"[^a-z0-9]+", " ", s).split() if t not in NOISE)


def strain_of(name):
    m = STRAIN.search(str(name or ""))
    if not m:
        return None
    t = next(g for g in m.groups() if g).lower()
    return {"s": "sativa", "i": "indica", "h": "hybrid"}.get(t, t)


def label_mg(label):
    label = str(label or "").strip().lower()
    if label in LABEL_MG:
        return LABEL_MG[label]
    m = LABEL_RE.match(label)
    if not m:
        return None
    return float(m.group(1)) * {"g": 1000, "mg": 1, "oz": 28349.5}[m.group(2)]


def size_key(product_type, weight_mg, weight_label, thc_display, name) -> str:
    """The size a price applies to, in a form both platforms agree on.

    Dosed kinds key on the THC dose ("100mgTHC"): Dutchie records a 100 mg
    drink's *net* weight (1000 mg) where Jane records its dose. Everything else
    keys on weight in mg, rounded to 50 mg, from weight_mg or a parsed label.
    A non-weight label ("24pk", "single") is no size at all.
    """
    if product_type in DOSED_TYPES:
        # The labelled dose in the name beats the measured figure some stores
        # report (98.04 mg for a 100 mg gummy); measured values round to 5 mg.
        text = str(name or "")
        doses = [float(x) for x in DOSE.findall(text)]
        for n, per in re.findall(r"(\d+)\s?x\s?(\d+(?:\.\d+)?)\s?mg", text, re.I):     # "10x10mg" -> 100
            doses.append(float(n) * float(per))
        if doses:
            return f"{max(doses):g}mgTHC"                 # the package total is the largest figure
        m = DOSE.search(str(thc_display or ""))
        if m:
            return f"{5 * round(float(m.group(1)) / 5):g}mgTHC"
        return ""
    v = weight_mg if (weight_mg is not None and not pd.isna(weight_mg) and weight_mg > 0) else label_mg(weight_label)
    return f"{int(round(v / 50))}mg" if v else ""


def pack_count(name) -> str:
    m = PACK.search(str(name or ""))
    return m.group(1) if m else ""


def as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.startswith("["):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return []
    return []


def slugify(text) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", str(text).lower())).strip("-")


# ----------------------------------------------------------------------------- stages

def load_products(db: Path) -> pd.DataFrame:
    conn = sqlite3.connect(db)
    df = pd.read_sql("SELECT id, product_id, name, brand_name, product_type, product_subcategory, strain_type, "
                     "weight_label, weight_mg, thc_display, price, dispensary_display, dispensary_slug, dispensary_url FROM products", conn)
    conn.close()
    df["platform"] = np.where(df.dispensary_url.str.contains("/shop/store/", na=False), "jane", "dutchie")
    return df.reset_index(drop=True)


def embed_text(df: pd.DataFrame) -> list[str]:
    t = (df.brand_name.fillna("") + " " + df.name.fillna("") + " | " + df.product_type.fillna("") + " "
         + df.product_subcategory.fillna("") + " | " + df.strain_type.fillna("") + " | " + df.weight_label.fillna("")
         + " | " + df.thc_display.fillna(""))
    return t.str.replace(r"\s+", " ", regex=True).str.strip().tolist()


def paths(db: Path) -> dict:
    stem = db.with_suffix("")
    return {k: Path(f"{stem}.{k}") for k in ("emb.npy", "ids.npy", "nnI.npy", "nnD.npy", "faiss")}


def stage_embed(db: Path) -> None:
    from sentence_transformers import SentenceTransformer     # torch: this process only
    df = load_products(db)
    t0 = time.time()
    emb = SentenceTransformer(MODEL).encode(embed_text(df), batch_size=64, normalize_embeddings=True, show_progress_bar=False).astype("float32")
    p = paths(db)
    np.save(p["emb.npy"], emb)
    np.save(p["ids.npy"], df["id"].to_numpy())
    print(f"embed: {emb.shape} in {time.time() - t0:.0f}s -> {p['emb.npy'].name}")


def stage_index(db: Path) -> None:
    import faiss                                             # faiss: this process only
    p = paths(db)
    emb = np.load(p["emb.npy"])
    index = faiss.IndexFlatIP(emb.shape[1])
    index.add(emb)
    faiss.write_index(index, str(p["faiss"]))
    t0 = time.time()
    D, I = index.search(emb, TOP_K + 1)
    np.save(p["nnI.npy"], I)
    np.save(p["nnD.npy"], D)
    print(f"index: top-{TOP_K} for {len(emb):,} rows in {time.time() - t0:.0f}s")


# --- prices --------------------------------------------------------------------

def latest(pattern: str) -> Path | None:
    files = glob.glob(str(PARENT / pattern))
    return Path(max(files, key=os.path.getmtime)) if files else None


def product_prices(df: pd.DataFrame, dutchie_csv: Path | None, vireo_csv: Path | None) -> pd.DataFrame:
    """One row per product per size. Falls back to the product's single price."""
    rows = []
    by_pid = df[df.platform == "dutchie"].set_index("product_id")
    if dutchie_csv is not None:
        d = pd.read_csv(dutchie_csv, usecols=["id", "Options", "recPrices", "recSpecialPrices"], low_memory=False)
        d["id"] = d["id"].astype(str)
        d = d[d["id"].isin(by_pid.index)]
        for pid, opts, prices, specials in zip(d["id"], d["Options"], d["recPrices"], d["recSpecialPrices"]):
            opts, prices, specials = as_list(opts), as_list(prices), as_list(specials)
            row = by_pid.loc[pid]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]
            for k, price in enumerate(prices):
                label = opts[k] if k < len(opts) else ""
                label = "" if str(label).upper() == "N/A" else str(label)
                sale = specials[k] if k < len(specials) and specials[k] not in (None, "", 0) else None
                rows.append((int(row["id"]), size_key(row.product_type, label_mg(label) or row.weight_mg, label or row.weight_label, row.thc_display, row["name"]),
                             label or row.weight_label, float(price), float(sale) if sale is not None else None))
    if vireo_csv is not None:
        v = pd.read_csv(vireo_csv, index_col=0, low_memory=False,
                        usecols=lambda c: c in {"product_id", "storeName", "bucket_price", "price_each", "amount"} | set(JANE_PRICE_COLS))
        v["key"] = v["product_id"].astype("Int64").astype(str) + "|" + v["storeName"].map(slugify)
        jane = df[df.platform == "jane"]
        jane_key = (jane.product_id.astype(str) + "|" + jane.dispensary_slug)
        lookup = dict(zip(jane_key, jane.index))
        for _, r in v.iterrows():
            i = lookup.get(r["key"])
            if i is None:
                continue
            row = df.loc[i]
            found = False
            for col, label in JANE_PRICE_COLS.items():
                if col in v and pd.notna(r[col]):
                    rows.append((int(row["id"]), size_key(row.product_type, label_mg(label), label, row.thc_display, row["name"]), label, float(r[col]), None))
                    found = True
            if not found:
                price = r["price_each"] if pd.notna(r.get("price_each")) else r["bucket_price"]
                if pd.notna(price):
                    rows.append((int(row["id"]), size_key(row.product_type, row.weight_mg, row.weight_label, row.thc_display, row["name"]), row.weight_label, float(price), None))
    prices = pd.DataFrame(rows, columns=["product_row", "size_key", "size_label", "price", "sale_price"])
    # products the CSVs didn't cover: their single price
    missing = df[~df["id"].isin(prices.product_row)]
    extra = pd.DataFrame({"product_row": missing["id"], "size_key": [size_key(*a) for a in zip(missing.product_type, missing.weight_mg, missing.weight_label, missing.thc_display, missing["name"])],
                          "size_label": missing.weight_label, "price": missing.price, "sale_price": None})
    return pd.concat([prices, extra], ignore_index=True).drop_duplicates(["product_row", "size_key", "size_label"])


# --- groups ----------------------------------------------------------------------

class Groups:
    """Union-find with group-level vetoes.

    A merge is refused if the two groups already contain the same dispensary
    (a store never lists one SKU twice), different strains, different formats,
    or different stated pack counts. Checking at the group level is what stops chains: A and C may
    each be compatible with an unmarked B while conflicting with each other.
    """

    def __init__(self, stores: pd.Series, strains, formats, packs=None):
        self.parent = np.arange(len(stores))
        self.stores = [{s} for s in stores]
        self.strains = [{x} if x else set() for x in strains]
        self.formats = [{x} if x else set() for x in formats]
        self.packs = [{x} if x else set() for x in (packs if packs is not None else [None] * len(stores))]
        self.method: dict[int, tuple[str, float]] = {}
        self.refused = 0

    @staticmethod
    def _clash(attr, ra, rb) -> bool:
        return bool(attr[ra]) and bool(attr[rb]) and attr[ra].isdisjoint(attr[rb])

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int, how: str, conf: float) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        if (self.stores[ra] & self.stores[rb]) or self._clash(self.strains, ra, rb) or self._clash(self.formats, ra, rb) \
                or self._clash(self.packs, ra, rb):
            self.refused += 1
            return False
        if len(self.stores[ra]) < len(self.stores[rb]):
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.stores[ra] |= self.stores[rb]
        self.strains[ra] |= self.strains[rb]
        self.formats[ra] |= self.formats[rb]
        self.packs[ra] |= self.packs[rb]
        for x in (a, b):
            if x not in self.method or self.method[x][1] > conf:
                self.method[x] = (how, conf)
        return True


def build_groups(df: pd.DataFrame, E: np.ndarray, I: np.ndarray, D: np.ndarray, dutchie_csv: Path | None) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    df["bn"] = df.brand_name.map(brand_norm)
    df["nn"] = [name_norm(n, b) for n, b in zip(df["name"], df.brand_name)]
    df["skey"] = [size_key(*a) for a in zip(df.product_type, df.weight_mg, df.weight_label, df.thc_display, df["name"])]
    # Pack count is a group-level veto, not part of the block: "10pk" appears in
    # some stores' names and not others for the same product, but a stated
    # 10-pack must never merge with a stated 20-pack.
    df["block"] = df.bn + "|" + df.product_type.fillna("") + "|" + df.skey
    df["pack"] = df["name"].map(pack_count)
    df["strain"] = df["name"].map(strain_of)
    df["format"] = df["name"].map(format_of)
    lib = None
    if dutchie_csv is not None:
        lib = pd.read_csv(dutchie_csv, usecols=["id", "libraryProductId"], low_memory=False).drop_duplicates("id")
        lib["id"] = lib["id"].astype(str)
        df = df.merge(lib.rename(columns={"id": "product_id", "libraryProductId": "lib"}), on="product_id", how="left")
        df.loc[df.platform == "jane", "lib"] = None
    else:
        df["lib"] = None
    nn, blocks, strain = df.nn.values, df.block.values, df.strain.values

    def jac(i, j):
        """Overlap of the *distinctive* name tokens; falls back to all tokens
        when a name is nothing but generic words ("Live Rosin")."""
        a, b = set(nn[i].split()), set(nn[j].split())
        da, db = a - GENERIC, b - GENERIC
        if not da or not db:
            da, db = a, b
        return len(da & db) / max(len(da | db), 1)

    def conflict(i, j):
        return strain[i] is not None and strain[j] is not None and strain[i] != strain[j]

    g = Groups(df.dispensary_display, df["strain"], df["format"], df["pack"])
    stats = {"exact_jane": 0, "name": 0, "library+name": 0, "fuzzy": 0}
    for _, rows in df[df.platform == "jane"].groupby("product_id").groups.items():
        rows = list(rows)
        for j in rows[1:]:
            stats["exact_jane"] += g.union(rows[0], j, "exact_jane", 1.0)
    df["dkey"] = np.where((df.bn != "") & (df.nn != ""), df.block + "|" + df.nn, None)
    for _, rows in df[df.dkey.notna()].groupby("dkey").groups.items():
        rows = list(rows)
        for j in rows[1:]:
            stats["name"] += g.union(rows[0], j, "name", 0.95)
    for _, rows in df[df.lib.notna()].groupby("lib").groups.items():
        rows = list(rows)
        for a in rows:
            for b in rows:
                if b <= a or g.find(a) == g.find(b) or blocks[a] != blocks[b] or conflict(a, b):
                    continue
                if jac(a, b) >= 0.8 and float(E[a] @ E[b]) >= 0.90:
                    stats["library+name"] += g.union(a, b, "library+name", 0.9)
    for i in range(len(df)):
        for j, s in zip(I[i][1:], D[i][1:]):
            if j <= i or blocks[j] != blocks[i] or g.find(i) == g.find(j) or conflict(i, j):
                continue
            if s >= 0.92 and jac(i, j) >= 0.8:
                stats["fuzzy"] += g.union(i, j, "fuzzy", float(s))
    stats["refused_same_store"] = g.refused
    roots = np.array([g.find(i) for i in range(len(df))])
    ids = {r: k for k, r in enumerate(np.unique(roots))}
    out = pd.DataFrame({"product_row": df["id"], "group_id": [ids[r] for r in roots],
                        "confidence": [g.method.get(i, ("single", 1.0))[1] for i in range(len(df))],
                        "method": [g.method.get(i, ("single", 1.0))[0] for i in range(len(df))]})
    return out, stats


def stage_groups(db: Path, dutchie_csv: Path | None, vireo_csv: Path | None, sample_out: Path | None) -> None:
    p = paths(db)
    df = load_products(db)
    E, ids, I, D = np.load(p["emb.npy"]), np.load(p["ids.npy"]), np.load(p["nnI.npy"]), np.load(p["nnD.npy"])
    if not np.array_equal(ids, df["id"].to_numpy()):
        sys.exit("embeddings are for a different set of rows -- run `embed` and `index` again")

    t0 = time.time()
    prices = product_prices(df, dutchie_csv, vireo_csv)
    print(f"prices: {len(prices):,} rows for {prices.product_row.nunique():,} products ({(prices.groupby('product_row').size() > 1).sum():,} multi-size) in {time.time() - t0:.0f}s")

    t0 = time.time()
    groups, stats = build_groups(df, E, I, D, dutchie_csv)
    print(f"groups: {stats} in {time.time() - t0:.0f}s")

    # per (group, size): each store's best price, then the stats across stores
    gp = prices.merge(groups[["product_row", "group_id"]], on="product_row").merge(df[["id", "dispensary_display"]], left_on="product_row", right_on="id")
    per_store = gp.groupby(["group_id", "size_key", "dispensary_display"]).price.min().reset_index()
    group_prices = per_store.groupby(["group_id", "size_key"]).price.agg(n_stores="size", min_price="min", median_price="median", avg_price="mean", max_price="max").reset_index()
    multi = group_prices[group_prices.n_stores >= 2]
    print(f"group_prices: {len(group_prices):,} (group, size) rows; {len(multi):,} at 2+ stores; median spread {(multi.max_price / multi.min_price).median():.2f}x")

    # similar: distinct products outside the row's own group. A neighbour with
    # the same brand and normalised name is the same product that couldn't be
    # grouped (a store listing it twice, say), not a similar one; and one
    # flavour at five stores is one suggestion, not five.
    gid = groups.group_id.to_numpy()
    bn = df.brand_name.map(brand_norm).to_numpy()
    nn = np.array([name_norm(n, b) for n, b in zip(df["name"], df.brand_name)])
    sim = []
    for i in range(len(df)):
        kept, seen = 0, {(bn[i], nn[i])}
        for j, s in zip(I[i][1:], D[i][1:]):
            key = (bn[j], nn[j])
            if gid[j] == gid[i] or key in seen:
                continue
            seen.add(key)
            sim.append((int(df["id"][i]), int(df["id"][j]), float(s), kept + 1))
            kept += 1
            if kept == KEEP_SIMILAR:
                break
    similar = pd.DataFrame(sim, columns=["product_row", "similar_row", "score", "rank"])

    conn = sqlite3.connect(db)
    try:
        prices.to_sql("product_prices", conn, if_exists="replace", index=False)
        groups.to_sql("product_groups", conn, if_exists="replace", index=False)
        group_prices.to_sql("group_prices", conn, if_exists="replace", index=False)
        similar.to_sql("similar_products", conn, if_exists="replace", index=False)
        for sql in ("CREATE INDEX IF NOT EXISTS idx_pp_row ON product_prices(product_row)",
                    "CREATE INDEX IF NOT EXISTS idx_pg_row ON product_groups(product_row)",
                    "CREATE INDEX IF NOT EXISTS idx_pg_group ON product_groups(group_id)",
                    "CREATE INDEX IF NOT EXISTS idx_gp ON group_prices(group_id, size_key)",
                    "CREATE INDEX IF NOT EXISTS idx_sp_row ON similar_products(product_row)"):
            conn.execute(sql)
        conn.commit()
    finally:
        conn.close()
    print(f"wrote product_prices, product_groups, group_prices, similar_products ({len(similar):,} rows) to {db.name}")

    if sample_out:
        # a labelling sample: random cross-store pairs from every non-exact tier
        rng = np.random.default_rng(0)
        m = groups.merge(df[["id", "name", "brand_name", "weight_label", "price", "dispensary_display", "platform"]], left_on="product_row", right_on="id")
        rows = []
        for tier, n in (("name", 40), ("library+name", 30), ("fuzzy", 30)):
            cands = m[(m.method == tier)]
            for gidx in rng.permutation(cands.group_id.unique()):
                members = m[m.group_id == gidx]
                if len(members) < 2:
                    continue
                a, b = members.sample(2, random_state=int(rng.integers(1 << 30))).itertuples()
                rows.append({"tier": tier, "confidence": round(min(a.confidence, b.confidence), 3), "a": f"[{a.platform[0]}] {a.brand_name} | {a.name} | {a.weight_label} | ${a.price} @ {a.dispensary_display}",
                             "b": f"[{b.platform[0]}] {b.brand_name} | {b.name} | {b.weight_label} | ${b.price} @ {b.dispensary_display}", "same_product": ""})
                if sum(r["tier"] == tier for r in rows) == n:
                    break
        pd.DataFrame(rows).to_csv(sample_out, index=False)
        print(f"precision sample: {len(rows)} pairs -> {sample_out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["embed", "index", "groups", "all"])
    ap.add_argument("--db", default=str(PROJECT_ROOT / "data" / "products_dev.db"))
    ap.add_argument("--dutchie-csv", default=None, help="default: newest ../all_dispensaries*.csv")
    ap.add_argument("--vireo-csv", default=None, help="default: newest ../vireo_products*.csv")
    ap.add_argument("--sample", default=None, help="write a labelling sample of cross-store pairs to this CSV")
    a = ap.parse_args()
    db = Path(a.db).resolve()
    if a.stage in ("embed", "index"):
        (stage_embed if a.stage == "embed" else stage_index)(db)
        return 0
    if a.stage == "all":
        for stage in ("embed", "index"):
            subprocess.run([sys.executable, __file__, stage, "--db", str(db)], check=True)
    dutchie = Path(a.dutchie_csv) if a.dutchie_csv else latest("all_dispensaries*.csv")
    vireo = Path(a.vireo_csv) if a.vireo_csv else latest("vireo_products*.csv")
    print(f"prices from: {dutchie and dutchie.name}, {vireo and vireo.name}")
    stage_groups(db, dutchie, vireo, Path(a.sample) if a.sample else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
