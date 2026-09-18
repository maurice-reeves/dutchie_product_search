"""The card-title split: strain out, descriptors folded, nothing scrambled."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from names import split_name  # noqa: E402


@pytest.mark.parametrize("name, brand, title, detail", [
    # the five from the design mock-up
    ("Malek's | 1g Live Resin Batter | Juicy (H)", "Malek's", "Juicy (H)", "Live resin batter"),
    ("Erva by In House Melts 90u First Press Live Rosin 1g - Intergalactic", "In House Melts", "Intergalactic", "Erva · 90u first press live rosin"),
    ("710 Labs | Deli Flower - TMZ #16", "710 Labs", "TMZ #16", "Deli flower"),
    ("Popcorn Shelf - Love Truffles (H)", "Cherry", "Love Truffles (H)", "Popcorn shelf"),
    ("REC: Craft Sour Diesel 510 Cartridge Distillate", "CRAFT", "Sour Diesel", "REC · 510 cartridge distillate"),
    # single segment split only at one end
    ("Wyld Gummies Hybrid Huckleberry 100mg", "Wyld", "Huckleberry", "Gummies hybrid"),
    ("Natty Rems | Gold Tip Live Resin Cart (H) Major Tom 1g", "Natty Rems", "Major Tom (H)", "Gold tip live resin cart"),
    ("Peach Hash Rosin Lemonade [2oz] (100mg)", "Journeyman", "Peach Hash Rosin Lemonade", ""),   # unknowns at both ends: left whole
    ("Edun Wilson x Super Peanut Butter Infused Joint", "Edun", "Wilson x Super Peanut Butter", "Infused joint"),
    # product first, descriptor after
    ("Black Maple #22 | 500MG | Rosin Cartridge", "Lazercat Cannabis", "Black Maple #22", "Rosin cartridge"),
    ("Apple Fizz OG | Distillate Cart", "CRAFT", "Apple Fizz OG", "Distillate cart"),
    # strain-type words and marks
    ("X Vape All-in-One 4g - Sativa Blackberry Gelato", "XVapes", "Blackberry Gelato", "Sativa · X vape all-in-one"),
    ("Cake Mix - H - Infused Blunt - PackWoods", "PACKS LOS ANGELES", "Cake Mix (H)", "Infused blunt · Packwoods"),
    ("Dro - Lemonade Bacio (S/H) - Popcorn", "Cannabis Brothers Holding Company LLC", "Lemonade Bacio (S/H)", "Dro · Popcorn"),
    # empty middle fields, brackets, ratios, "The"
    ("Dabble Extracts - 1g Sugar Wax - - Moona Lisa's Smile (H)", "Dabble", "Moona Lisa's Smile (H)", "Extracts · Sugar wax"),
    ("100mg - Blood Orange Focus 1:1:1 - Rosin Gummies  (10pk)", "Dialed In Gummies", "Blood Orange Focus 1:1:1", "Rosin gummies"),
    ("The Deli - Blue Ritz #22", "All Pro Farms", "Blue Ritz #22", "Deli"),
    ("Gelato Cake [4g]", "Kush Masters", "Gelato Cake", ""),
    ("Blue Dream", "", "Blue Dream", ""),
    # batch numbers stay with the strain; lone strain words describe; [I] is a mark like (I)
    ("Motorbreath #15", "Natty Rems", "Motorbreath #15", ""),
    ("Sour Peach - Sativa [10pk] (100mg)", "Smokiez", "Sour Peach", "Sativa"),
    ("Wee Joints - 5.0g - Prerolls - Hubba Bubba [I] (10PK)", "Joints", "Hubba Bubba (I)", "Wee · Prerolls"),
])
def test_split_name(name, brand, title, detail):
    assert split_name(name, brand) == (title, detail)


def test_never_returns_an_empty_title():
    assert split_name("Wyld", "Wyld") == ("Wyld", "")                # the name is just the brand
    assert split_name("Live Resin Cartridge", "Brand")[0] == "Live Resin Cartridge"   # all descriptor words
    assert split_name("", "x") == ("", "")                            # nothing in, nothing out
