"""Colour -> demand. Neutral colours (black, navy, brown, olive...) are what most
buyers want; burgundy, bright red or yellow on the same jacket sells slower and
for less. Brands can override (Carhartt: brown/black great, burgundy weak).
"""

from __future__ import annotations

import re
import unicodedata

# Colour family -> words (LT, PL, FI, DE, FR, ES, IT, LV, ET, EN). Matched at
# word start, so "czarn" finds czarna/czarny, "bordow" finds bordowa.
FAMILIES: dict[str, list[str]] = {
    "burgundy": ["burgundy", "bordo", "bordow", "bordeaux", "burgund", "maroon", "wine", "vyšn",
                 "viininpunai", "weinrot", "granate", "bordeau"],
    "black": ["black", "juod", "czarn", "schwarz", "noir", "negr", "=nero", "=nera", "meln", "=musta"],
    "white": ["white", "balt", "biał", "bial", "valkoi", "weiß", "weiss", "blanc", "bianc", "valge",
              "off-white", "ecru"],
    "grey": ["grey", "gray", "pilk", "szar", "harmaa", "grau", "gris", "grigi", "pelēk", "=hall",
             "anthracite", "antracit", "grafit", "graphite", "charcoal"],
    "navy": ["navy", "tamsiai mėlyn", "granat", "laivastonsinin", "marine", "dunkelblau", "bleu marine",
             "blu scuro", "tumši zil", "tumesinin"],
    "blue": ["blue", "mėlyn", "niebiesk", "sinin", "blau", "bleu", "azul", "=blu", "zil", "sinine"],
    "brown": ["brown", "=ruda", "=rudas", "=rudos", "=rudi", "=rudo", "=rudą", "brąz", "braz", "ruskea", "braun", "marron", "marrón", "brun", "marrone",
              "brūn", "pruun", "hamilton", "chocolate", "šokolad", "hickory",
              "chestnut", "mocha", "espresso", "dune", "burnt cedar", "kašton", "kasztan"],
    "beige": ["beige", "smėl", "beż", "=beżowa", "kremin", "krem", "cream", "=sand", "camel", "=tan",
              "kamel", "taupe"],
    "olive": ["olive", "khaki", "chaki", "oliv", "alyvuog", "khak", "military"],
    "green": ["green", "žali", "=žalia", "=žalias", "zielon", "vihre", "grün", "=vert", "verte", "verde", "zaļ", "rohel"],
    "red": ["=red", "raudon", "czerwon", "punai", "=rot", "=rote", "=roter", "rouge", "=rojo", "=roja", "=rosso", "=rossa", "sarkan", "punane"],
    "yellow": ["yellow", "geltona", "geltonas", "żółt", "zolt", "keltai", "gelb", "jaune", "amarill",
               "giall", "dzelten", "kollane", "mustard", "garstyč"],
    "orange": ["orange", "oranž", "pomarańcz", "oranssi", "naranj", "arancion", "oranž"],
    "pink": ["pink", "rožin", "różow", "roosa", "pinkki", "rosa", "rose", "rozā", "fuchsia", "magenta"],
    "purple": ["purple", "lilac", "lavender", "violet", "fiolet", "liliow", "lila", "purpur", "levand",
               "alyvin", "mauve"],
    "multi": ["multicolor", "multicolour", "įvairiaspal", "kolorow", "monivär", "bunt", "multicolore",
              "camo", "kamufliaž", "moro"],
}

# Demand multiplier by colour family for each kind of item. 1.0 = typical.
TYPE_GROUPS = {
    "outerwear": {"puffer", "jacket", "shell", "coat", "fleece", "vest"},
    "tops": {"hoodie", "sweater", "tshirt", "shirt", "tracksuit"},
    "shoes": {"shoes", "boots"},
    "bottoms": {"pants", "jeans", "shorts"},
}
DEMAND: dict[str, dict[str, float]] = {
    "outerwear": {"black": 1.1, "navy": 1.0, "grey": 1.0, "brown": 1.05, "beige": 1.0, "olive": 1.05,
                  "blue": 0.95, "green": 0.9, "white": 0.85, "orange": 0.9, "yellow": 0.85,
                  "red": 0.8, "burgundy": 0.75, "multi": 0.85, "pink": 0.9, "purple": 0.85},
    "tops": {"black": 1.05, "white": 1.05, "grey": 1.0, "navy": 1.0, "beige": 1.0, "brown": 1.0,
             "olive": 1.0, "blue": 0.95, "green": 0.95, "red": 0.9, "burgundy": 0.85,
             "yellow": 0.85, "orange": 0.85, "multi": 0.9, "pink": 0.95, "purple": 0.9},
    "shoes": {"black": 1.05, "white": 1.05, "beige": 1.0, "brown": 1.0, "grey": 1.0, "navy": 0.95,
              "olive": 0.95, "blue": 0.9, "green": 0.85, "red": 0.85, "burgundy": 0.85,
              "yellow": 0.8, "orange": 0.8, "multi": 0.85, "pink": 0.9, "purple": 0.85},
    "bottoms": {"black": 1.0, "blue": 1.0, "navy": 1.0, "beige": 1.0, "olive": 1.0, "brown": 1.0,
                "grey": 1.0, "white": 0.9, "green": 0.9, "red": 0.8, "burgundy": 0.8,
                "yellow": 0.8, "orange": 0.8, "multi": 0.85, "pink": 0.9, "purple": 0.85},
}

# Families checked first win ties at the same position ("tamsiai mėlyna" -> navy).
_ORDER = list(FAMILIES)


def _norm(s: str) -> str:
    return unicodedata.normalize("NFC", s or "").lower()


def _strip(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _pattern(words: list[str]) -> re.Pattern:
    """Word-start match; "=word" means the whole word only ("=blu" must not
    match "bluza", the Polish word for hoodie; "=tan" not "tank")."""
    parts = []
    for w in sorted(words, key=lambda w: len(w.lstrip("=")), reverse=True):
        if w.startswith("="):
            parts.append(rf"(?<![\w]){re.escape(_norm(w[1:]))}(?![\w])")
        else:
            parts.append(rf"(?<![\w]){re.escape(_norm(w))}")
    return re.compile("|".join(parts), re.UNICODE)


_RX = {fam: _pattern(words) for fam, words in FAMILIES.items()}


def family(text: str) -> str | None:
    """First colour mentioned in `text`, as a family ('Bordo' -> 'burgundy')."""
    t = _norm(text)
    best: tuple[int, int, int] | None = None
    fam_best = None
    for i, fam in enumerate(_ORDER):
        for variant in (t, _strip(t)):
            m = _RX[fam].search(variant)
            if m:
                key = (m.start(), -(m.end() - m.start()), i)
                if best is None or key < best:
                    best, fam_best = key, fam
                break
    return fam_best


def type_group(category: str) -> str | None:
    for group, cats in TYPE_GROUPS.items():
        if category in cats:
            return group
    return None


def popular(category: str, brand_colours: dict | None = None, n: int = 3) -> list[str]:
    """The colours that sell best for this brand (or type)."""
    table = brand_colours or DEMAND.get(type_group(category) or "", {})
    return [c for c, _ in sorted(table.items(), key=lambda kv: -kv[1])[:n]]


def demand(fam: str | None, category: str, brand_colours: dict | None = None) -> float:
    if not fam:
        return 1.0
    if brand_colours and fam in brand_colours:
        return float(brand_colours[fam])
    group = type_group(category)
    return DEMAND.get(group, {}).get(fam, 1.0) if group else 1.0
