"""Deterministic tests for the parts of build_similarity that encode the
matching rules. No embeddings, no database: these are the normalisers and the
group-level vetoes that the precision check was calibrated against."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_similarity as bs  # noqa: E402


@pytest.mark.parametrize("name,brand,expected", [
    ("Wyld Marionberry Gummies 10x10mg 100mg", "Wyld", "marionberry gummies"),        # generic words stay here; jac() drops them
    ("Dialed In | Rosin Gummies | Rainbow Berries (H) 100mg", "Dialed In Gummies", "in rosin rainbow berries h"),  # 2-letter brand words are left alone
    ("100mg - Bear Creek Lake #2627 - Rosin Gummies (10pk)", "Dialed In Gummies", "bear creek lake rosin"),   # "gummies" is a brand word here
    ("Classic Assorted Flavors [10-Pack]", "Wana", "classic assorted flavors 10 pack"),
])
def test_name_norm_strips_sizes_brand_and_batch_numbers(name, brand, expected):
    assert bs.name_norm(name, brand) == expected


def test_name_norm_keeps_variant_words_in_parentheses():
    assert bs.name_norm("S1 | Satchel Bag (Black)", "Schedule 1") != bs.name_norm("S1 | Satchel Bag (Tan)", "Schedule 1")
    assert bs.name_norm("Classic Rosin Gummies (Indica)", "Dialed In") != bs.name_norm("Classic Rosin Gummies (Hybrid)", "Dialed In")


@pytest.mark.parametrize("name,strain", [
    ("Gasonade (H) 1g", "hybrid"), ("Maple Nectar-(I)-FS Live Rosin", "indica"), ("Peach Bellini - Sativa [10pk]", "sativa"),
    ("Blue Dream", None), ("Assorted Flavors 10:1 CBD:THC Gummies", "cbd"),
])
def test_strain_markers(name, strain):
    assert bs.strain_of(name) == strain


@pytest.mark.parametrize("name,fmt", [
    ("Black Label All-In-One - Sour Power OG", "aio"), ("1000mg Live Resin Black Label Cartridge", "cart"),
    ("Live Diamond Disposable - Maui Waui", "aio"), ("Rove Live Diamonds Starter Kit 1g", "kit"), ("Blue Dream", None),
])
def test_format_words(name, fmt):
    assert bs.format_of(name) == fmt


def test_size_key_uses_dose_for_edibles_and_canonical_mg_otherwise():
    # Dutchie: 100 mg drink with a 1000 mg net weight; Jane: same drink with dose only
    assert bs.size_key("Edible", 1000.0, "100mg", "100mg", "Pineapple Papaya Drink 100mg") == "100mgTHC"
    assert bs.size_key("Edible", None, "", "100mg", "Pineapple Papaya (100mg)") == "100mgTHC"
    # measured THC (98.04 mg) on a labelled 100 mg gummy, and 100mgTHC written as one token
    assert bs.size_key("Edible", None, "", "98.04mg", "Wyld - Gummies - Marionberry (I)") == "100mgTHC"
    assert bs.size_key("Edible", None, "", "", "WYLD | 100mgTHC Gummies 10pk | Marionberry (I)") == "100mgTHC"
    assert bs.name_norm("WYLD | 100mgTHC Gummies 10pk | Marionberry (I)", "Wyld") == "gummies marionberry i"
    assert bs.size_key("Edible", None, "", "100mg", "Marionberry Gummies 10x10mg 100mg") == "100mgTHC"
    assert bs.size_key("Edible", None, "", "", "Joy Bombs Sour Fruit - 10mg THC (4pk / 2.5mg)") == "10mgTHC"
    # a 1g cart with weight_mg, and one with only the label, agree
    assert bs.size_key("Vaporizers", 1000.0, "1g", "", "Tiger's Blood Cart") == bs.size_key("Vaporizers", None, "1g", "", "Tiger's Blood") == "20mg"
    assert bs.size_key("Flower", None, "1/8oz", "24%", "Head Hunter") == "70mg"
    # a pack count is not a weight
    assert bs.size_key("Accessories", None, "24pk", "", "Ultra Thin Cones [24pk]") == ""


def test_pack_count():
    assert bs.pack_count("Limoncello - Hybrid [10pk] (100mg)") == "10"
    assert bs.pack_count("Limoncello - Hybrid [20pk] (100mg)") == "20"
    assert bs.pack_count("Blue Dream 1g") == ""


def test_groups_refuse_same_store_strain_and_format_clashes():
    stores = pd.Series(["A", "B", "C", "A", "D", "E"])
    strains = ["hybrid", None, "sativa", None, None, None]
    formats = [None, None, None, "cart", "aio", None]
    names = ["x 1g", "x", "x", "x 2g", "x", "x"]
    g = bs.Groups(stores, strains, formats, names=names)
    assert g.union(0, 1, "fuzzy", 0.95)            # hybrid + unmarked: fine
    assert not g.union(1, 2, "fuzzy", 0.95)        # would chain sativa into the hybrid group -> refused
    assert not g.union(0, 3, "fuzzy", 0.95)        # store A lists two different things -> refused
    assert g.union(3, 5, "fuzzy", 0.95)            # cart + unmarked: fine
    assert not g.union(5, 4, "fuzzy", 0.95)        # would chain an aio into the cart group -> refused
    assert g.refused == 3
    assert g.find(0) == g.find(1) and g.find(0) != g.find(2)


def test_groups_allow_a_store_listing_the_same_thing_twice():
    """A store's duplicate entries (med + rec, a re-listed SKU) share a name;
    that overlap must not keep the product's stores apart."""
    stores = pd.Series(["A", "A", "B", "A"])
    names = ["blue dream 1g", "blue dream 1g", "blue dream 1g", "blue dream 2g"]
    g = bs.Groups(stores, [None] * 4, [None] * 4, names=names)
    assert g.union(0, 1, "duplicate", 1.0)         # the duplicate itself
    assert g.union(2, 1, "name", 0.95)             # store B joins a group that holds A twice: fine
    assert not g.union(0, 3, "name", 0.95)         # A's 2g is a variant, not a duplicate -> refused
    assert g.find(0) == g.find(1) == g.find(2) != g.find(3)


def test_groups_refuse_different_textures_but_allow_overlap():
    g = bs.Groups(pd.Series(["A", "B", "C"]), [None] * 3, [None] * 3, None,
                  textures=[{"resin"}, {"resin", "badder"}, {"sugar"}])
    assert g.union(0, 1, "fuzzy", 0.95)            # live resin + live resin badder: overlap, fine
    assert not g.union(1, 2, "fuzzy", 0.95)        # live sugar is a different product -> refused
    assert bs.textures_of("Live Resin Badder - Blue Dream") == {"resin", "badder"}
    assert bs.textures_of("Blue Dream 1g") == frozenset()


def test_groups_refuse_different_stated_pack_counts_but_allow_unstated():
    g = bs.Groups(pd.Series(["A", "B", "C"]), [None] * 3, [None] * 3, ["10", "", "20"])
    assert g.union(0, 1, "fuzzy", 0.95)            # 10pk + unstated: fine
    assert not g.union(1, 2, "fuzzy", 0.95)        # would bring a 20pk into the 10pk group -> refused


def test_groups_record_the_weakest_tier_per_row():
    g = bs.Groups(pd.Series(["A", "B", "C"]), [None] * 3, [None] * 3)
    g.union(0, 1, "name", 0.95)
    g.union(1, 2, "fuzzy", 0.93)
    assert g.method[0] == ("name", 0.95)
    assert g.method[1] == ("fuzzy", 0.93)          # row 1 joined by both; the weaker link is what it's worth
