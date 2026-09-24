"""Card titles: the strain or product name pulled out of a menu listing, and
everything else the listing said folded into one descriptor line.

    "Malek's | 1g Live Resin Batter | Juicy (H)"        -> "Juicy",           "live resin batter"
    "REC: Craft Sour Diesel 510 Cartridge Distillate"   -> "Sour Diesel",     "REC · 510 cartridge distillate"
    "Wyld Gummies Hybrid Huckleberry 100mg"             -> "Huckleberry",     "gummies hybrid"

Menus write names every which way, so this is a heuristic, built on the
vocabulary build_similarity.py already uses to tell category words from
product words: the brand and any sizes are removed, the name is cut into
segments at | / - : and brackets, and the segment made up mostly of words
the vocabulary does *not* know is the product; the rest is the descriptor.
A single-segment name is split only when its unknown words sit together at
one end ("Sour Diesel 510 Cartridge Distillate", "Gummies Hybrid Huckleberry");
otherwise it is left whole rather than scrambled. Strain marks -- "(H)",
"[I]", "(S/I)", a lone "H" segment, even a truncated "(I" -- are dropped:
the card has no use for them and `strain_type` carries the fact. The
original listing name is kept in `name` for search and the popup's title.

import_products.py adds `display_name` and `display_detail` to every row;
`python names.py --backfill data/products.db` adds them to an existing
database without touching row ids (the popup tables stay valid).
"""
from __future__ import annotations

import re
import sqlite3
import sys

# build_similarity.py has no heavy imports at module level (torch and faiss
# load inside their stages), so this is safe from the venv.
from build_similarity import BATCH, GENERIC, NOISE, SIZE_TOKENS

# Words that describe what a thing *is* rather than which one it is. GENERIC
# is the matcher's list; these are the extra ones menus use in listings.
DESCRIPTOR = GENERIC | set("""batter budder deli popcorn shelf smalls small bulk press first cold cured fresh frozen
extracts extract persy jar jars twist twists blend blends gold tip tips ounce quarter eighth half
crumble terp terps sauce sugar wax honey bucket ambrosia select reserve value bronze silver platinum
craft house artisan signature limited exclusive collection line rec med medical recreational
infused pack prepack pre-roll preroll prerolls rolls roll joints joint blunt blunts mini king size
cartridge cart carts pod pods disposable disposables aio all-in-one vape vapes distillate liquid
diamonds live rosin resin solventless hash cbd thc cbn cbg cbc ratio drops syrup mints mint chews
soft chew gummies gummy candy candies chocolate chocolates cookie cookies brownie brownies bar bars
drink drinks beverage beverages soda cola tea coffee capsule capsules tablet tablets tincture tinctures
topical topicals cream creams balm balms lotion salve patch patches roll-on bath oil oils
flower buds bud nug nugs shake trim indoor outdoor greenhouse sungrown organic
sativa indica hybrid h s i sh ih si is hi hs""".split())
# "(H)", "[I]", "(S/H)", "(S,H,I)", "(IH)", "(Sativa)", and "(I" when the menu
# cut the name off before the bracket closed. Three letters need separators:
# "(ish)" and "Ish" are words.
STRAIN_MARK = re.compile(r"[(\[]\s*(?:[his](?:\s*[/,]\s*[his]){0,2}|[his]{2}|hybrid|indica|sativa|cbd)\s*(?:[)\]]|$)", re.I)
LONE_MARK = re.compile(r"(?i)[his](?:[/,][his]){0,2}|[his]{2}")
# "(CBD/THC 4:1)", "(CBD | THC)", "(CBD:THC)": a cannabinoid ratio, which the
# separator split would otherwise cut in half. It moves to the descriptor.
RATIO_MARK = re.compile(r"\(\s*((?:cbd|thc|cbn|cbg|cbc)(?:\s*[/|:]\s*(?:cbd|thc|cbn|cbg|cbc))+[^)]*)\)", re.I)
STRAIN_WORD = re.compile(r"^(sativa|indica|hybrid)$", re.I)
BRACKET_SIZE = re.compile(r"\[[^\]]*\]|\((?=[^)]*\d)[^)]*(?:mg|g|oz|ml|pk|pack|ct|piece|count|x)\b[^)]*\)", re.I)
REC_MED = re.compile(r"^\s*(rec|med|medical|recreational)\s*[:\-–—]\s*", re.I)
SEPARATORS = re.compile(r"\s*(?:\||//|/(?!\d)| - | – | — |:\s+|\s{2,})\s*")
WORD = re.compile(r"[a-z0-9']+")


def _brand_pattern(brand: str) -> re.Pattern | None:
    chunks = re.findall(r"[a-z0-9]+", str(brand or "").lower())
    if not chunks:
        return None
    body = r"[^a-z0-9]*".join(map(re.escape, chunks))
    return re.compile(rf"(?<![a-z0-9])(?:by\s+)?{body}(?![a-z0-9])", re.I)


# Words a brand's record carries that its menu listings usually drop:
# "Green Dot Labs" is listed as "Green Dot | ...", "Slow Burn Farms, LLC" as
# "Slow Burn Farms - ...". Only ever trimmed off the end of a brand name.
BRAND_SUFFIX = set("""llc inc co company corp corporation ltd labs lab laboratories cannabis extracts
extract extraction edibles edible gummies collective medicinals signature products brands papers
concentrates farm farms nurseries brand industries supply vape wax rec""".split())
BRAND_SPLIT = re.compile(r"\s+(?:by|-|–|—|\|)\s+", re.I)


def _brand_aliases(brand: str) -> set[str]:
    """Shorter names the listing may use for this brand, as lowercase word
    tuples joined by spaces: "Green Dot Labs" -> {"green dot"}, "Joy Bombs by
    Joyibles" -> {"joy bombs", "joyibles"}. The full brand is not included;
    split_name strips that anywhere already."""
    full = " ".join(re.findall(r"[a-z0-9']+", str(brand or "").lower()))
    out = set()
    for part in [str(brand or "")] + BRAND_SPLIT.split(str(brand or "")):
        words = re.findall(r"[a-z0-9']+", part.lower())
        if words[:1] == ["the"]:
            words = words[1:]
        while words:
            out.add(" ".join(words))
            if words[-1] not in BRAND_SUFFIX:
                break
            words = words[:-1]
    out.discard(full)
    return {a for a in out if a}


def _strip_aliases(segments: list[str], aliases: set[str]) -> list[str]:
    """Drop a short brand name that is a whole segment, or that opens the
    first segment or closes the last. Only at those edges: a brand word inside
    a name ("Black Maple #22" from Maple Concentrates) is part of the product."""
    if not aliases or not segments:
        return segments
    key = lambda s: " ".join(_words(s))
    kept = [s for s in segments if key(s) not in aliases]
    if not kept:
        return segments
    for i, at_start in ((0, True), (len(kept) - 1, False)):
        words = kept[i].split()
        for n in range(len(words) - 1, 0, -1):
            chunk = words[:n] if at_start else words[-n:]
            if key(" ".join(chunk)) in aliases:
                rest = words[n:] if at_start else words[:-n]
                if not at_start and rest[-1:] and rest[-1].lower() == "by":   # "Lick N Laid Pre-Roll by Greenfields"
                    rest = rest[:-1]
                kept[i] = " ".join(rest)
                break
    return [s for s in kept if s and _words(s)]


def _known(word: str) -> bool:
    w = word.lower()
    if w == "x":                     # a cross ("Wilson x Peanut Butter"), sizes being gone already
        return False
    return w in DESCRIPTOR or w in NOISE or bool(re.fullmatch(r"\d+[a-z]*", w))


def _words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _clean(text: str) -> str:
    text = re.sub(r"\s*-\s*-\s*", " - ", text)          # "Wax - - Blue Dream" (an empty middle field)
    text = re.sub(r"^[\s\-–—:|,./]+|[\s\-–—:|,./]+$", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _detail_case(text: str) -> str:
    out = []
    for w in text.split():
        out.append(w if (w.isupper() and len(w) <= 4) or any(c.isdigit() for c in w) else w.lower())
    s = " ".join(out)
    return s[:1].upper() + s[1:] if s else s


def split_name(name: str, brand: str = "") -> tuple[str, str]:
    """(title, descriptor) for one listing. Never returns an empty title."""
    raw = str(name or "").strip()
    if not raw:
        return "", ""
    text, tags = raw, []
    m = REC_MED.match(text)
    if m:
        tags.append(m.group(1).upper()[:3]); text = text[m.end():]
    pattern = _brand_pattern(brand)
    if pattern:
        text = pattern.sub(" ", text)
    ratios = [re.sub(r"\s+", " ", m.strip()) for m in RATIO_MARK.findall(text)]
    text = RATIO_MARK.sub(" ", text)
    text = STRAIN_MARK.sub(" ", text)
    text = BRACKET_SIZE.sub(" ", text)
    text = SIZE_TOKENS.sub(" ", text)
    segments = [_clean(s) for s in SEPARATORS.split(text)]
    segments = [s for s in segments if s and _words(s)]
    # A segment that is only a strain letter ("Cake Mix - H - Infused Blunt")
    # is a mark too; a lone "Sativa" segment is a descriptor word.
    segments = [s for s in segments if not LONE_MARK.fullmatch(s)]
    segments = _strip_aliases(segments, _brand_aliases(brand))

    title, detail_parts = "", []
    if not segments:
        title = _clean(STRAIN_MARK.sub(" ", SIZE_TOKENS.sub(" ", raw))) or raw
    elif len(segments) == 1:
        words = segments[0].split()
        # A batch number (#15) names the product as much as the word before it.
        flags = [bool(BATCH.fullmatch(w)) or not _known(re.sub(r"[^a-z0-9']", "", w.lower())) for w in words]
        lead = 0
        while lead < len(flags) and flags[lead]:
            lead += 1
        trail = 0
        while trail < len(flags) - lead and flags[-1 - trail]:
            trail += 1
        if lead and not trail and lead < len(words):
            title, detail_parts = " ".join(words[:lead]), [" ".join(words[lead:])]
        elif trail and not lead and trail < len(words):
            title, detail_parts = " ".join(words[-trail:]), [" ".join(words[:-trail])]
        else:
            title = segments[0]
    else:
        def score(seg):
            ws = _words(seg)
            unk = sum(not _known(w) for w in ws)
            return (unk / len(ws), unk)
        best = max(range(len(segments)), key=lambda k: (score(segments[k]), k))
        title = segments[best]
        detail_parts = [s for k, s in enumerate(segments) if k != best]
    # A strain-type word at either end of the title describes, not names.
    tw = title.split()
    while tw and STRAIN_WORD.match(tw[0]):
        detail_parts.insert(0, tw.pop(0))
    while tw and STRAIN_WORD.match(tw[-1]):
        detail_parts.append(tw.pop())
    title = " ".join(tw) or title
    parts = [re.sub(r"(?i)^the\s+", "", _clean(p)) for p in detail_parts]
    detail = " · ".join(tags + [_detail_case(p) for p in parts if p and _words(p) != ["the"]] + ratios)
    return _clean(title) or raw, detail


def backfill(db_path: str) -> int:
    """Add display_name/display_detail to an existing database in place."""
    conn = sqlite3.connect(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(products)")}
    for col in ("display_name", "display_detail"):
        if col not in cols:
            conn.execute(f"ALTER TABLE products ADD COLUMN {col} TEXT")
    rows = conn.execute("SELECT id, name, brand_name FROM products").fetchall()
    conn.executemany("UPDATE products SET display_name = ?, display_detail = ? WHERE id = ?",
                     [(*split_name(n, b), i) for i, n, b in rows])
    conn.commit(); conn.close()
    return len(rows)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--backfill":
        print(f"display names written for {backfill(sys.argv[2]):,} products")
    else:
        for arg in sys.argv[1:] or ["Malek's | 1g Live Resin Batter | Juicy (H)"]:
            print(split_name(arg))
