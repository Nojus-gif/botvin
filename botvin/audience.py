"""Who will buy this? Gender x size x colour -> how big the resale market is.

A pink jacket in L is effectively a women's item, and L is an uncommon
women's size, so it will sell slower than the brand and price suggest.
"""

from __future__ import annotations

import re
import unicodedata

WOMEN_WORDS = [
    "women", "womens", "women's", "woman", "ladies", "lady", "female",
    "moter", "moteriš", "moteriški", "moteriška",          # LT
    "damsk", "damsk", "kobiec", "dla kobiet",              # PL
    "naisten", "nais",                                    # FI
    "damen", "frauen",                                    # DE
    "femme", "femmes", "féminin",                          # FR
    "mujer", "donna", "dames", "sieviešu", "naiste",       # ES IT NL LV ET
]
MEN_WORDS = [
    "men", "mens", "men's", "man's", "male",
    "vyr", "vyriš", "vyriški", "vyriška",                  # LT
    "męsk", "mesk", "dla mężczyzn",                        # PL
    "miesten",                                            # FI
    "herren", "männer",                                   # DE
    "homme", "hommes", "masculin",                        # FR
    "hombre", "uomo", "heren", "vīriešu", "meeste",        # ES IT NL LV ET
]

# Colours whose market is mostly women. Only colour families that change who
# buys are listed; black/navy/grey/beige/olive/etc. sell to everyone.
FEMININE_COLOURS = {
    "pink": ["pink", "rožin", "rožinė", "różow", "roosa", "pinkki", "rosa", "rose", "rozā", "fuksij",
             "fuchsia", "magenta"],
    "lilac": ["lilac", "lavender", "purple", "violet", "violetin", "fiolet", "liliow", "lila",
              "purpur", "violetti", "levand", "alyv", "mauve"],
}

# Demand by size for each audience (letter sizes). 1.0 = the common sizes.
SIZE_DEMAND = {
    "women": {"xs": 0.9, "s": 1.0, "m": 1.0, "l": 0.75, "xl": 0.55, "xxl": 0.4},
    "men": {"xs": 0.5, "s": 0.85, "m": 1.0, "l": 1.0, "xl": 0.95, "xxl": 0.8},
}


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFC", s or "").lower().replace("’", "'")
    return s


def _strip(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _rx(words: list[str]) -> re.Pattern:
    alts = "|".join(re.escape(_norm(w)) for w in sorted(set(words), key=len, reverse=True))
    return re.compile(rf"(?<![\w])(?:{alts})", re.UNICODE)


_WOMEN_RX, _MEN_RX = _rx(WOMEN_WORDS), _rx(MEN_WORDS)
_COLOUR_RX = {name: _rx(words) for name, words in FEMININE_COLOURS.items()}


def _has(rx: re.Pattern, text: str) -> bool:
    return bool(rx.search(text) or rx.search(_strip(text)))


def gender_from_text(text: str) -> str | None:
    t = _norm(text)
    w, m = _has(_WOMEN_RX, t), _has(_MEN_RX, t)
    if w and not m:
        return "women"
    if m and not w:
        return "men"
    return None                               # neither, or "unisex"-ish both


def feminine_colour(text: str) -> str | None:
    t = _norm(text)
    for name, rx in _COLOUR_RX.items():
        if _has(rx, t):
            return name
    return None


def shoe_demand(gender: str, eu: float) -> float:
    if gender == "women":
        return 1.0 if eu <= 40 else 0.8 if eu <= 41 else 0.55
    if eu < 40:
        return 0.55
    if eu < 41 or eu >= 46:
        return 0.8 if eu < 47 else 0.65
    return 1.0


def assess(*, title: str, description: str, size_letter: str | None, shoe_eu: float | None,
           page_gender: str | None = None, page_colour: str | None = None) -> tuple[float, str | None, list[str]]:
    """Return (multiplier, gender, flags) for how well this gender/size/colour resells."""
    text = f"{title} {description}"
    colour = feminine_colour(page_colour or "") or feminine_colour(text)
    gender = page_gender or gender_from_text(text)
    source = "listed" if (page_gender or gender) else None
    if gender is None and colour:
        gender, source = "women", "colour"   # pink/lilac with no other clue: women will buy it

    flags: list[str] = []
    mult = 1.0
    if gender:
        label = "women's" if gender == "women" else "men's"
        flags.append(f"{label} ({'by colour' if source == 'colour' else 'listed'})")
        if size_letter:
            sd = SIZE_DEMAND[gender].get(size_letter, 1.0)
            if sd < 1.0:
                flags.append(f"size {size_letter.upper()} is uncommon for {gender} x{sd:.2f}")
            mult *= sd
        elif shoe_eu is not None:
            sd = shoe_demand(gender, shoe_eu)
            if sd < 1.0:
                flags.append(f"shoe size {shoe_eu:g} is uncommon for {gender} x{sd:.2f}")
            mult *= sd
    if colour:
        if gender == "men":
            mult *= 0.75                     # men's item in pink/lilac: narrow market
            flags.append(f"{colour} men's item x0.75")
        else:
            flags.append(f"colour: {colour}")
    return round(mult, 3), gender, flags
