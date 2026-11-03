"""Resale price estimation: brand table x category x season x condition,
blended with live asking prices the bot has observed for comparable items."""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Item

DATA_FILE = Path(__file__).parent / "data" / "brands.yaml"

# Category keywords, multilingual. Order matters: first match wins, so the more
# specific categories (puffer, shell) come before the generic ones (jacket).
# Each keyword is matched at the START of a word, so "kurtk" matches "kurtka",
# "kurtki", "kurtką", and "takki" matches "untuvatakki" via the puffer rule.
CATEGORY_KEYWORDS: list[tuple[str, list[str]]] = [
    ("puffer", ["dūnu", "~dūnu", "sulejope", "~untejope", "talvejope", "ziemas jaka", "puffer", "down jacket", "down", "puch", "pūk", "pliumin", "~untuva",
                "~daunen", "~dunjack", "~dunjakke", "doudoune", "piumino", "plumas", "zimow", "žiemin",
                "talvitakki", "winterjacke", "insulated"]),
    ("shell", ["hardshell", "shell", "kurtka przeciwdeszcz", "regenjacke",
               "sadetakki", "waterproof"]),
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
               "schuhe", "chaussures", "scarpe", "zapatillas", "air max", "dunk", "550", "990"]),
    ("bag", ["soma", "mugursoma", "kott", "seljakott", "bag", "backpack", "torba", "plecak", "kuprin", "rankin", "reppu", "laukku",
             "rucksack", "tasche", "sac", "borsa", "mochila"]),
    ("accessory", ["cap", "hat", "beanie", "czapk", "kepur", "pipo", "mütze", "scarf",
                   "szalik", "šalik", "gloves", "rękawiczk", "pirštin", "belt", "pasek", "diržas"]),
]

CONDITION_MULT = {
    "new_with_tags": 1.25,
    "new_without_tags": 1.15,
    "very_good": 1.00,
    "good": 0.90,
    "satisfactory": 0.70,
    "unknown": 0.90,
}

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


class PriceModel:
    def __init__(self, data_file: Path | str = DATA_FILE, brand_overrides: dict | None = None,
                 filters: dict | None = None):
        filters = filters or {}
        self.exclude_kids = bool(filters.get("exclude_kids", True))
        self.excluded_sizes = {normalize(x) for x in
                               filters.get("excluded_sizes", DEFAULT_EXCLUDED_SIZES)}
        self.min_shoe_size = float(filters.get("min_shoe_size", 42))
        self.max_shoe_size = float(filters.get("max_shoe_size", 46))
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
        t = normalize(text)
        for cat, rx in self._category_res:
            if _contains(rx, t):
                return cat
        return "other"

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
            size_txt = normalize(item.size)
            # Prefer an explicit EU size ("US 10 / EU 44"), else the first 2-digit number.
            m = (re.search(r"eu\s*(\d{2}(?:[.,]5)?)", size_txt)
                 or re.search(r"(?<![\d.,])(\d{2}(?:[.,]5)?)(?![\d])", size_txt))
            if m:
                eu = float(m.group(1).replace(",", "."))
                if eu < self.min_shoe_size or eu > self.max_shoe_size:
                    return f"shoe size {item.size}"
        return None

    def season_mult(self, category: str, when: dt.date | None = None) -> float:
        month = (when or dt.date.today()).month
        curve = self.seasonality[self.category_season.get(category, "neutral")]
        return curve[month - 1]

    # ---------------------------------------------------------------- estimate
    def estimate(self, item: Item, market: tuple[float | None, int] = (None, 0),
                 when: dt.date | None = None) -> Estimate:
        text = normalize(f"{item.title} {item.description}")
        flags: list[str] = []

        brand, brand_from_field = self.match_brand(item)
        category = self.detect_category(item.title) if item.title else "other"

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

        premium_mult = 1.0
        for kw, rx in self._premium_res.items():
            if _contains(rx, text):
                spec = self.premium[kw]
                premium_mult = max(premium_mult, float(spec.get("mult", 1.0)))
                if spec.get("category") and category in ("other", "jacket"):
                    category = spec["category"]
                flags.append(f"model: {kw}")

        if category in overrides:
            base = float(overrides[category])
        else:
            base = float(info["base"]) * self.category_factors.get(category, self.category_factors["other"])

        season = self.season_mult(category, when)
        cond = CONDITION_MULT.get(item.condition, CONDITION_MULT["unknown"])
        size = SIZE_MULT.get(letter_size(item.size) or "", 1.0)

        mult = premium_mult * season * cond * size
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
                        min(confidence, 1.0), flags)
