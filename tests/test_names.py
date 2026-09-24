"""The card-title split: strain out, descriptors folded, nothing scrambled."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from names import split_name  # noqa: E402


@pytest.mark.parametrize("name, brand, title, detail", [
    # the five from the design mock-up
    ("Malek's | 1g Live Resin Batter | Juicy (H)", "Malek's", "Juicy", "Live resin batter"),
    ("Erva by In House Melts 90u First Press Live Rosin 1g - Intergalactic", "In House Melts", "Intergalactic", "Erva · 90u first press live rosin"),
    ("710 Labs | Deli Flower - TMZ #16", "710 Labs", "TMZ #16", "Deli flower"),
    ("Popcorn Shelf - Love Truffles (H)", "Cherry", "Love Truffles", "Popcorn shelf"),
    ("REC: Craft Sour Diesel 510 Cartridge Distillate", "CRAFT", "Sour Diesel", "REC · 510 cartridge distillate"),
    # single segment split only at one end
    ("Wyld Gummies Hybrid Huckleberry 100mg", "Wyld", "Huckleberry", "Gummies hybrid"),
    ("Natty Rems | Gold Tip Live Resin Cart (H) Major Tom 1g", "Natty Rems", "Major Tom", "Gold tip live resin cart"),
    ("Peach Hash Rosin Lemonade [2oz] (100mg)", "Journeyman", "Peach Hash Rosin Lemonade", ""),   # unknowns at both ends: left whole
    ("Edun Wilson x Super Peanut Butter Infused Joint", "Edun", "Wilson x Super Peanut Butter", "Infused joint"),
    # product first, descriptor after
    ("Black Maple #22 | 500MG | Rosin Cartridge", "Lazercat Cannabis", "Black Maple #22", "Rosin cartridge"),
    ("Apple Fizz OG | Distillate Cart", "CRAFT", "Apple Fizz OG", "Distillate cart"),
    # strain-type words and marks
    ("X Vape All-in-One 4g - Sativa Blackberry Gelato", "XVapes", "Blackberry Gelato", "Sativa · X vape all-in-one"),
    ("Cake Mix - H - Infused Blunt - PackWoods", "PACKS LOS ANGELES", "Cake Mix", "Infused blunt · Packwoods"),
    ("Dro - Lemonade Bacio (S/H) - Popcorn", "Cannabis Brothers Holding Company LLC", "Lemonade Bacio", "Dro · Popcorn"),
    ("Litties - Preroll 10 Pk - Day & Night (S/I)", "Litties", "Day & Night", "Preroll"),
    ("Popcorn Shelf -  Bernie hanna butter (I", "", "Bernie hanna butter", "Popcorn shelf"),      # cut off mid-mark
    ("X Vape | 1g  Cart | Papaya Dream (S", "X Vape", "Papaya Dream", "Cart"),
    ("Roobie Snacks-(I/S/H)-Infused Joint 3pk- -RVRS", "RVRS", "Roobie Snacks", "Infused joint"),
    ("Kaviar | 3g Infused Preroll 5pk | Variety (S,H,I)", "Kaviar", "Variety", "Infused preroll"),
    ("O.Pen Ish", "O.pen", "Ish", ""),                                   # a word, not three marks
    # a cannabinoid ratio in brackets is one descriptor, not a separator
    ("Grön | Pearls (CBD/THC 4:1) Pomegranate 100mg", "Grön", "Pomegranate", "Pearls · CBD/THC 4:1"),
    ("TasteBudz Rosin Gummies 100mg - Pineapple 1:1 (CBD | THC) - 100mg:100mg", "TasteBudz", "Pineapple 1:1", "Rosin gummies · CBD | THC"),
    # empty middle fields, brackets, ratios, "The"
    ("Dabble Extracts - 1g Sugar Wax - - Moona Lisa's Smile (H)", "Dabble", "Moona Lisa's Smile", "Extracts · Sugar wax"),
    ("100mg - Blood Orange Focus 1:1:1 - Rosin Gummies  (10pk)", "Dialed In Gummies", "Blood Orange Focus 1:1:1", "Rosin gummies"),
    ("The Deli - Blue Ritz #22", "All Pro Farms", "Blue Ritz #22", "Deli"),
    ("Gelato Cake [4g]", "Kush Masters", "Gelato Cake", ""),
    ("Blue Dream", "", "Blue Dream", ""),
    # batch numbers stay with the strain; lone strain words describe; [I] is a mark like (I), dropped
    ("Motorbreath #15", "Natty Rems", "Motorbreath #15", ""),
    ("Sour Peach - Sativa [10pk] (100mg)", "Smokiez", "Sour Peach", "Sativa"),
    ("Wee Joints - 5.0g - Prerolls - Hubba Bubba [I] (10PK)", "Joints", "Hubba Bubba", "Wee · Prerolls"),
    # the listing uses a short form of the brand record's name
    ("Green Dot | 3.5g Bud | Otoro (H)", "Green Dot Labs", "Otoro", "Bud"),
    ("Bonanza | 1g LR Cart | Black Cherry Soda (S)", "Bonanza Cannabis", "Black Cherry Soda", "LR cart"),
    ("Coda 100mg - Coffee and Doughnuts Milk Chocolate", "Coda Signature", "Coffee and Doughnuts Milk Chocolate", ""),
    ("Tropical Fruit | 100MG | Joy Bombs", "Joy Bombs by Joyibles", "Tropical Fruit", ""),
    ("Joyibles | Joybomb Fire Bomb | 10mg", "Joy Bombs by Joyibles", "Joybomb Fire Bomb", ""),
    ("Slow Burn Farms - Pre-roll - Hybrid - Dante's Wrath", "Slow Burn Farms, LLC", "Dante's Wrath", "Pre-roll · Hybrid"),
    ("Sugar Chunk + Butter Pecan | 1.25G Infused Preroll |Trichome Collective", "The Trichome Collective", "Sugar Chunk + Butter Pecan", "Infused preroll"),
    ("Lick N Laid Pre-Roll by Greenfields", "Greenfields Cannabis Co.", "Lick N Laid", "Pre-roll"),
    # ...but only at the edges: inside the name a brand word belongs to the product
    ("Black Maple #22 | 500MG | Rosin Cartridge", "Maple Concentrates", "Black Maple #22", "Rosin cartridge"),
])
def test_split_name(name, brand, title, detail):
    assert split_name(name, brand) == (title, detail)


def test_never_returns_an_empty_title():
    assert split_name("Wyld", "Wyld") == ("Wyld", "")                # the name is just the brand
    assert split_name("Green Dot", "Green Dot Labs") == ("Green Dot", "")   # ...or a short form of it
    assert split_name("Live Resin Cartridge", "Brand")[0] == "Live Resin Cartridge"   # all descriptor words
    assert split_name("", "x") == ("", "")                            # nothing in, nothing out
