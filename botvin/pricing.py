"""Resale price estimation: brand table x category x season x condition,
blended with live asking prices the bot has observed for comparable items."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import colours
from .audience import assess
from .models import Item

DATA_FILE = Path(__file__).parent / "data" / "brands.yaml"

# Category keywords, multilingual. Order matters: first match wins, so the more
# specific categories (puffer, shell) come before the generic ones (jacket).
# Each keyword is matched at the START of a word, so "kurtk" matches "kurtka",
# "kurtki", "kurtką", and "takki" matches "untuvatakki" via the puffer rule.
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("puffer", ["sulejope", "~untejope", "talvejope", "ziemas jaka", "puffer", "down jacket",
                "~untuva", "~daunen", "~dunjack", "~dunjakke", "doudoune", "piumino", "plumas",
                "talvitakki", "winterjacke"]),
    ("shell", ["hardshell", "shell", "torrentshell", "regenjacke", "sadetakki", "~kuoritakki"]),
    ("vest", ["vest", "gilet", "bezrękaw", "liemen", "liivi", "weste", "smanicato"]),
    ("fleece", ["flīs", "~flīs", "fleece", "polar", "flis", "fliis", "fleecejacke", "polaire"]),
    ("coat", ["mētel", "mantel", "coat", "płaszcz", "palt", "mantel", "manteau", "cappotto", "abrigo", "parka", "trench"]),
    ("jacket", ["jaka", "jaciņa", "jope", "~jope", "jacket", "kurtk", "striuk", "~takki", "~jacke", "~jacka", "~jakke", "veste", "blouson",
                "giacca", "giubbotto", "chaqueta", "chamarra", "jas", "bunda", "anorak",
                "windbreaker", "softshell"]),
    ("hoodie", ["džemperis ar kapuci", "kapuutsiga", "hoodie", "hoody", "bluza", "džemperis su gobtuvu", "huppari", "kapuzen", "sweat"]),
    ("sweater", ["džemperis", "kampsun", "svīteris", "sweater", "jumper", "sweter", "megztin", "neule", "pullover", "pull",
                 "maglion", "jersey", "cardigan", "knit", "quarter zip", "1/4 zip", "half zip",
                 "džempe"]),
    ("tshirt", ["t-krekls", "t-särk", "t-shirt", "tshirt", "tee", "koszulk", "marškinėl", "t-paita", "paita"]),
    ("shirt", ["marškin", "krekls", "särk", "shirt", "koszul", "polo", "hemd", "chemise", "camicia", "camisa"]),
    ("shorts", ["shorts", "spoden", "šort", "shortsit", "short"]),
    ("jeans", ["jeans", "jeansy", "džins", "farkut", "denim"]),
    ("pants", ["bikses", "püksid", "pants", "trousers", "spodnie", "kelnės", "kelnes", "housut", "hose",
               "pantalon", "pantaloni", "cargo", "joggers"]),
    ("boots", ["zābak", "saapad", "boots", "boot", "buty zimowe", "batai žieminiai", "kozaki", "saappaat",
               "stiefel", "bottes", "trapery"]),
    ("shoes", ["apavi", "kurpes", "botas", "tossud", "jalatsid", "shoes", "sneakers", "trainers", "buty", "batai", "sportbačiai", "kengät",
               "schuhe", "chaussures", "scarpe", "zapatillas", "air max", "dunk", "550", "990",
               "samba", "gazelle", "spezial", "superstar", "stan smith", "yeezy", "air force",
               "af1", "vomero", "p-6000", "2002r", "9060", "1906r", "xt-6", "xt6", "speedcross",
               "gel-kayano", "gel-lyte", "tabi"]),
    ("bag", ["soma", "mugursoma", "kott", "seljakott", "bag", "backpack", "torba", "plecak", "kuprin", "rankin", "reppu", "laukku",
             "rucksack", "tasche", "sac", "borsa", "mochila"]),
    ("accessory", ["cap", "hat", "beanie", "czapk", "kepur", "pipo", "mütze", "scarf",
                   "szalik", "šalik", "gloves", "rękawiczk", "pirštin", "belt", "pasek", "diržas"]),
]

# Words that describe a jacket rather than name an item ("kurtka ZIMOWA",
# "PUCHOWA kurtka"). They only turn a jacket/coat into a puffer/shell -- so
# "rękawiczki zimowe" (winter gloves) stay gloves.
MODIFIERS: list[tuple[str, list[str]]] = [
    ("puffer", ["dūnu", "~dūnu", "down", "puch", "pūk", "pliumin", "zimow", "žiemin", "ziemas",
                "insulated", "winter", "talve"]),
    ("shell", ["przeciwdeszcz", "waterproof", "neperšlamp", "gore-tex", "goretex"]),
]
UPGRADABLE = {"jacket", "coat", "other"}

CONDITION_MULT = {
    "new_with_tags": 1.25,
    "new_without_tags": 1.15,
    "very_good": 1.00,
    "good": 0.90,
    "satisfactory": 0.70,
    "unknown": 0.90,
}

MAX_BOOST = 1.6


def _prod(xs) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out


SIZE_MULT = {"s": 0.95, "m": 1.0, "l": 1.0, "xl": 1.0}

# Letter sizes that sell slowly -> skipped by default (configurable).
DEFAULT_EXCLUDED_SIZES = ["xxs", "xs", "xxl", "2xl", "xxxl", "3xl", "xxxxl", "4xl", "5xl", "6xl"]

# Kids' sizes on Vinted: "140 cm", "9-10 m.", "12 years", "8 lat", "6 ans", "18 mėn."...
_AGE_UNITS = (r"m\.|m|metų|metai|mėn\.?|years?|yrs?|y|lat|lata|ans|jahre|años|anni|vuotta|"
              r"months?|mois|mo|mies\.?|miesięcy|kk|gadi|aastat")
_KIDS_SIZE_RES = [
    re.compile(r"\b(\d{2,3})\s*cm\b"),                                      # body height in cm
    re.compile(rf"\b\d{{1,2}}(?:\s*[-/]\s*\d{{1,2}})?\s*(?:{_AGE_UNITS})(?![\w])"),
    re.compile(r"\b(?:kids?|junior|youth|baby|toddler)\b"),
]
_LETTER_SIZE_RE = re.compile(r"^\s*((?:\d)?x{0,4}[sml]|\dxl|x{0,4}l)\b")


def kids_size(size: str) -> bool:
    s = normalize(size)
    if not s:
        return False
    m = _KIDS_SIZE_RES[0].search(s)
    if m and 40 <= int(m.group(1)) <= 176:
        return True
    return bool(_KIDS_SIZE_RES[1].search(s) or _KIDS_SIZE_RES[2].search(s))


def shoe_eu(size: str) -> float | None:
    """'US 10 / EU 44' -> 44.0; '42,5' -> 42.5. Prefers an explicit EU value."""
    s = normalize(size)
    m = (re.search(r"eu\s*(\d{2}(?:[.,]5)?)", s)
         or re.search(r"(?<![\d.,])(\d{2}(?:[.,]5)?)(?![\d])", s))
    return float(m.group(1).replace(",", ".")) if m else None


def letter_size(size: str) -> str | None:
    """'XXL / 44 / 16' -> 'xxl'; '42' -> None."""
    m = _LETTER_SIZE_RE.match(normalize(size))
    return m.group(1) if m else None


def normalize(text: str) -> str:
    """Lowercase; keep diacritics (needed for LT/PL keywords) but unify quotes."""
    text = unicodedata.normalize("NFC", text or "").lower()
    return text.replace("’", "'").replace("`", "'")


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def _word_prefix_pattern(words: list[str]) -> re.Pattern:
    """Match any keyword at the start of a word. A keyword written as "~abc"
    matches anywhere instead, for compound-word languages (FI/DE/SV):
    "~takki" finds "untuvatakki", "~jacke" finds "Winterjacke"."""
    parts = []
    for w in sorted(words, key=lambda w: len(w.lstrip("~")), reverse=True):
        if w.startswith("~"):
            parts.append(re.escape(normalize(w[1:])))
        else:
            parts.append(rf"(?<![\w]){re.escape(normalize(w))}")
    return re.compile("|".join(parts), re.UNICODE)


def _contains(pattern: re.Pattern, text: str) -> bool:
    return bool(pattern.search(text) or pattern.search(_strip_accents(text)))


@dataclass
class Estimate:
    brand: str | None
    category: str
    resale: float                 # expected resale price (EUR)
    baseline: float               # static-table value before market blending
    market_median: float | None   # median observed asking price, if any
    market_samples: int
    confidence: float             # 0..1
    flags: list[str] = field(default_factory=list)
    excluded: str | None = None   # reason, if the item should be skipped
    min_profit: float | None = None   # brand-specific stricter thresholds
    min_margin: float | None = None
    max_buy: float | None = None      # brand-specific price ceiling (UGG: 20)
    instant: bool = False             # brand sells within minutes
    gender: str | None = None         # women / men, if known
    colour_family: str | None = None  # black, navy, burgundy...
    fast_seller: bool = False         # learned: this kind of item sells within the review window
    learned_factor: float = 1.0


class PriceModel:
    def __init__(self, data_file: Path | str = DATA_FILE, brand_overrides: dict | None = None,
                 filters: dict | None = None):
        filters = filters or {}
        self.exclude_kids = bool(filters.get("exclude_kids", True))
        self.excluded_sizes = {normalize(x) for x in
                               filters.get("excluded_sizes", DEFAULT_EXCLUDED_SIZES)}
        self.min_shoe_size = float(filters.get("min_shoe_size", 36))
        self.max_shoe_size = float(filters.get("max_shoe_size", 47))
        with open(data_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if brand_overrides:
            data["brands"].update(brand_overrides)
        self.category_factors: dict[str, float] = data["category_factors"]
        self.seasonality: dict[str, list[float]] = data["seasonality"]
        self.category_season: dict[str, str] = data["category_season"]
        self.brands: dict[str, dict] = data["brands"]
        self.damage_re = _word_prefix_pattern(data.get("damage_keywords", []))
        self.fake_re = _word_prefix_pattern(data.get("fake_keywords", []))
        self.kids_re = _word_prefix_pattern(data.get("kids_keywords", []))
        self.premium = {normalize(k): v for k, v in (data.get("premium_keywords") or {}).items()}
        self._premium_res = {k: _word_prefix_pattern([k]) for k in self.premium}
        self._category_res = [(cat, _word_prefix_pattern(words)) for cat, words in CATEGORY_KEYWORDS]
        self._modifier_res = [(cat, _word_prefix_pattern(words)) for cat, words in MODIFIERS]

        self._alias_res: list[tuple[str, re.Pattern]] = []
        for name, info in self.brands.items():
            aliases = set(info.get("aliases") or []) | {name}
            alts = "|".join(re.escape(normalize(a)) for a in sorted(aliases, key=len, reverse=True))
            self._alias_res.append((name, re.compile(rf"(?<![\w])(?:{alts})(?![\w])", re.UNICODE)))

    # ------------------------------------------------------------------ lookup
    def match_brand(self, item: Item) -> tuple[str | None, bool]:
        """Return (brand, came_from_structured_brand_field)."""
        brand_field = normalize(item.brand)
        if brand_field:
            for name, rx in self._alias_res:
                if _contains(rx, brand_field):
                    return name, True
            # The seller picked a brand we don't track. Don't fall back to the
            # title: sellers stuff titles with other brands for search
            # ("aku ... lowa scarpa merrell mammut").
            if brand_field not in ("", "other", "kita", "inna", "autre", "andere", "no brand",
                                   "be prekės ženklo", "bez marki", "sans marque"):
                return None, False
        title = normalize(item.title)
        for name, rx in self._alias_res:
            if _contains(rx, title):
                return name, False
        return None, False

    def detect_category(self, text: str) -> str:
        """The item word that appears FIRST in the title decides the type
        ("Rękawiczki zimowe the north face" -> gloves, not a jacket)."""
        t = normalize(text)
        variants = (t, _strip_accents(t))
        best: tuple[int, int, int] | None = None
        category = "other"
        for order, (cat, rx) in enumerate(self._category_res):
            for v in variants:
                m = rx.search(v)
                if m:
                    key = (m.start(), -(m.end() - m.start()), order)
                    if best is None or key < best:
                        best, category = key, cat
                    break
        if category in UPGRADABLE:
            for cat, rx in self._modifier_res:
                if _contains(rx, t):
                    return cat
        return category

    # "a la UGG", "UGG style", "tipo Moncler", "kaip Arcteryx", "w stylu Stone Island"...
    _STYLE_BEFORE = (r"a\s*'?\s*la|ala|à\s+la|style|type|typu|typ|w\s+typie|tipo|tipas|kaip|panaš\w*\s+į|w\s+stylu|"
                     r"jak|like|look\s*alike|inspired\s+by|in\s+the\s+style\s+of|im\s+stil\s+von|"
                     r"genre|façon|stil")
    _STYLE_AFTER = r"style|type|tipo|stiliaus|look|like|inspired|stil|styl"

    def brand_mismatch(self, brand: str, text: str) -> str | None:
        """Listing tagged with a brand it isn't: the title names a different
        (usually cheaper) brand, or says it's only in that brand's style."""
        info = self.brands.get(brand) or {}
        others = info.get("other_brands") or []
        if others and _contains(_word_prefix_pattern([o for o in others]), text):
            hit = next(o for o in others if _contains(_word_prefix_pattern([o]), text))
            return f"says '{hit}', not {brand}"
        aliases = set(info.get("aliases") or []) | {brand}
        alts = "|".join(re.escape(normalize(a)) for a in sorted(aliases, key=len, reverse=True))
        rx = re.compile(rf"(?<![\w])(?:(?:{self._STYLE_BEFORE})\s*[-:]?\s*(?:{alts})(?![\w])"
                        rf"|(?:{alts})[\s-]*(?:{self._STYLE_AFTER})(?![\w]))", re.UNICODE)
        if _contains(rx, text):
            return f"only '{brand}'-style"
        return None

    def size_exclusion(self, item: Item, category: str, text: str) -> str | None:
        """Reason to skip the item because of its size / being for kids."""
        if self.exclude_kids:
            if kids_size(item.size):
                return f"kids size ({item.size})"
            if _contains(self.kids_re, text):
                return "kids item"
        letter = letter_size(item.size)
        if letter and letter in self.excluded_sizes:
            return f"extreme size ({item.size})"
        if category in ("shoes", "boots"):
            eu = shoe_eu(item.size)
            if eu is not None:
                if eu < self.min_shoe_size or eu > self.max_shoe_size:
                    return f"shoe size {item.size}"
        return None

    def season_mult(self, category: str, when: dt.date | None = None,
                    brand: str | None = None) -> float:
        month = (when or dt.date.today()).month
        season = (self.brands.get(brand) or {}).get("season") if brand else None
        curve = self.seasonality[season or self.category_season.get(category, "neutral")]
        return curve[month - 1]

    # ---------------------------------------------------------------- estimate
    def estimate(self, item: Item, market: tuple[float | None, int] = (None, 0),
                 when: dt.date | None = None) -> Estimate:
        text = normalize(f"{item.title} {item.description}")
        flags: list[str] = []

        brand, brand_from_field = self.match_brand(item)
        category = self.detect_category(item.title) if item.title else "other"
        if category == "other" and brand and self.brands[brand].get("default_category"):
            category = self.brands[brand]["default_category"]      # "UGG Tazz" -> shoes

        mismatch = self.brand_mismatch(brand, text) if brand else None
        if mismatch:
            return Estimate(brand, category, 0, 0, None, 0, 0, [], excluded=mismatch)

        fake_text = text
        for word in (self.brands.get(brand) or {}).get("not_fake_words") or []:
            fake_text = fake_text.replace(normalize(word), " ")   # e.g. Margiela "Replica" line
        if _contains(self.fake_re, fake_text):
            return Estimate(brand, category, 0, 0, None, 0, 0, ["possible fake"], excluded="fake keywords")
        if not brand:
            return Estimate(None, category, 0, 0, None, 0, 0, [], excluded="unknown brand")
        skip = self.size_exclusion(item, category, text)
        if skip:
            return Estimate(brand, category, 0, 0, None, 0, 0, [], excluded=skip)

        info = self.brands[brand]
        overrides = info.get("overrides") or {}
        if category in (info.get("skip_categories") or []):
            return Estimate(brand, category, 0, 0, None, 0, 0, [],
                            excluded=f"{brand} {category}s are skipped (low demand)")

        premium_mult = 1.0
        # Model names only count in the TITLE: descriptions often mention other
        # models ("like a Detroit"), which made an Anchorage parka look like one.
        title_text = normalize(item.title)
        for kw, rx in self._premium_res.items():
            if _contains(rx, title_text):
                spec = self.premium[kw]
                premium_mult = max(premium_mult, float(spec.get("mult", 1.0)))
                if spec.get("category") and category in ("other", "jacket"):
                    category = spec["category"]
                flags.append(f"model: {kw}")

        if category in overrides:
            base = float(overrides[category])
        else:
            base = float(info["base"]) * self.category_factors.get(category, self.category_factors["other"])

        season = self.season_mult(category, when, brand)
        cond = CONDITION_MULT.get(item.condition, CONDITION_MULT["unknown"])
        letter = letter_size(item.size)
        aud_mult, gender, aud_flags = assess(
            title=item.title, description=item.description, size_letter=letter,
            shoe_eu=shoe_eu(item.size) if category in ("shoes", "boots") else None,
            page_gender=item.gender or None, page_colour=item.colour or None)
        # Known audience: size demand depends on gender. Unknown: generic table.
        size = aud_mult if gender else SIZE_MULT.get(letter or "", 1.0) * aud_mult
        flags.extend(aud_flags)

        # Colour vs. what sells for this brand/type. Page colour first, then
        # the title (descriptions are too noisy). Pink/lilac is handled above
        # as a women's item, so it isn't penalised twice.
        fam = colours.family(item.colour) if item.colour else None
        fam = fam or colours.family(item.title)
        colour_mult = 1.0
        if fam and not (fam in ("pink", "purple") and gender != "men"):
            table = info.get("colours")
            if table and info.get("colours_for") and colours.type_group(category) not in info["colours_for"]:
                table = None                    # Nike's sneaker colours don't apply to its jackets
            colour_mult = colours.demand(fam, category, table)
            if colour_mult < 0.97 or colour_mult > 1.03:
                popular = colours.popular(category, table)
                note = f"colour {fam} x{colour_mult:.2f}"
                if colour_mult < 1 and popular:
                    note += f" (best sellers: {', '.join(popular)})"
                flags.append(note)
        size *= colour_mult

        # Bonuses (model x season x colour x new-with-tags) multiply up fast; a
        # black Nuptse in November came out at 2x base. Cap the combined boost.
        factors = [premium_mult, season, cond, size]
        boost = min(_prod(max(f, 1.0) for f in factors), MAX_BOOST)
        mult = boost * _prod(min(f, 1.0) for f in factors)
        if boost == MAX_BOOST:
            flags.append(f"bonuses capped at x{MAX_BOOST}")
        if _contains(self.damage_re, text):
            mult *= 0.75
            flags.append("damage mentioned")
        if _contains(self.kids_re, text):        # only reached if exclude_kids is off
            mult *= 0.5
            flags.append("kids item?")
        if season >= 1.1:
            flags.append(f"in season x{season:.2f}")

        baseline = base * mult

        # Blend with what comparable items are listed for right now. Asking
        # prices overshoot real sale prices, so discount them a bit.
        median, n = market
        resale = baseline
        if median and n >= 5:
            w = min(0.5, n / 60)
            resale = (1 - w) * baseline + w * (median * 0.85 * cond)

        confidence = 0.55
        if brand_from_field:
            confidence += 0.2          # brand comes from the structured field
        if category != "other":
            confidence += 0.15
        if n >= 5:
            confidence += 0.1
        return Estimate(brand, category, round(resale, 2), round(baseline, 2), median, n,
                        min(confidence, 1.0), flags,
                        min_profit=info.get("min_profit"), min_margin=info.get("min_margin"),
                        max_buy=info.get("max_buy"),
                        instant=bool(info.get("instant")) or any(
                            _contains(_word_prefix_pattern([m]), title_text)
                            for m in info.get("instant_models") or []),
                        gender=gender, colour_family=fam)
