"""Extract upload time and description from a Vinted item page (HTML).

Search results no longer carry the upload time, but the item page still shows
it ("Įkelta prieš 19 val." / "Uploaded 25 minutes ago"). We try, in order:
  1. machine-readable timestamps embedded in the page's JSON,
  2. the human "uploaded X ago" text, in any of the languages Vinted uses.
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import re
import time

TS_KEYS = ("created_at_ts", "uploaded_at_ts", "published_at_ts", "created_ts")
ISO_KEYS = ("created_at", "uploaded_at", "published_at", "first_published_at")

# "N <unit>" with the unit word in many languages -> seconds per unit.
UNITS = [
    # minutes
    (r"min(?:ute|utes|uč|učių|utė|utes|uty|ut|utę|uuttia|uutti|uten|\.)?", 60),
    (r"sek(?:und[ėeyžęa]*|\.)?|sec(?:ond)?s?|s(?:ekuntia)?", 1),
    # hours
    (r"val(?:and[ąaų]?|\.)?|h(?:ours?|rs?)?|hod(?:in[ay]?)?|godz(?:in[aęy]?)?|"
     r"stunden?|std\.?|heures?|or[ae]|horas?|uur|tunti(?:a)?|timm?(?:ar|er)?", 3600),
    # days
    (r"d(?:ien[ąųos]*|ays?|ni|n[ií]|ag(?:e|en)?|\.)?|jours?|giorn[oi]|d[ií]as?|päivää?|dag(?:ar|e)?", 86400),
    # weeks
    (r"sav(?:ait[ėęes]*|\.)?|weeks?|tyg(?:odni[ea]?)?|wochen?|semaines?|settiman[ae]|semanas?|"
     r"viikko(?:a)?|veckor?", 604800),
    # months
    (r"mėn(?:es[iį]ų?|\.)?|months?|miesi[aą]c[ey]?|monate?n?|mois|mes[ei]s?|kuukau(?:si|tta)", 2592000),
    # years ("m." in LT is metai)
    (r"m\.?|met(?:ai|us|ų)|years?|lat[a]?|rok|jahren?|ans?|años?|ann[oi]|vuo(?:si|tta)|år", 31536000),
]
UNIT_RES = [(re.compile(rf"^(?:{p})$", re.I), secs) for p, secs in UNITS]

# The number is optional: Vinted writes "Įkelta prieš val." (an hour ago),
# "prieš min.", "Uploaded a day ago". "a/an/one" or no number means 1.
_N = r"(\d+|an?|one|jeden|jedna|ein(?:e|em|er)?|un[ea]?|yksi)?\s*"
_U = r"([^\W\d_][^\s<,]*?\.?)"
# Only phrases anchored on the "uploaded" label. A bare "26 minutes ago"
# elsewhere on the page (the seller's other items, reviews) is ignored.
REL_PATTERNS = [
    re.compile(rf"(?:įkelta|ikelta)\s+prieš\s+{_N}{_U}(?=[\s.,<]|$)", re.I),            # LT
    re.compile(rf"(?:uploaded|added|posted|listed)\s+{_N}{_U}\s+ago", re.I),            # EN
    re.compile(rf"(?:dodan[eoy]|dodano)\s+{_N}{_U}\s+temu", re.I),                      # PL
    re.compile(rf"(?:hochgeladen|eingestellt)\s+vor\s+{_N}{_U}(?=[\s.,<]|$)", re.I),    # DE
    re.compile(rf"(?:ajouté|publié)\s+il\s+y\s+a\s+{_N}{_U}(?=[\s.,<]|$)", re.I),     # FR
    re.compile(rf"(?:subido|publicado)\s+hace\s+{_N}{_U}(?=[\s.,<]|$)", re.I),          # ES
    re.compile(rf"(?:caricato|pubblicato)\s+{_N}{_U}\s+fa", re.I),                       # IT
    re.compile(rf"(?:lisätty|ladattu)\s+{_N}{_U}\s+sitten", re.I),                       # FI
    re.compile(rf"(?:augšupielādēts|pievienots)\s+(?:pirms\s+)?{_N}{_U}(?=[\s.,<]|$)", re.I),  # LV
]
JUST_NOW = re.compile(r"(?:įkelta|ikelta)\s+(?:dabar|ką\s+tik)|(?:uploaded|added|listed)\s+just\s+now|"
                      r"dodan[eoy]\s+przed\s+chwilą|(?:hochgeladen|eingestellt)\s+gerade|"
                      r"ajouté\s+à\s+l'instant|lisätty\s+juuri", re.I)


def _unit_seconds(word: str) -> int | None:
    w = word.strip().lower()
    for cand in (w, w.rstrip(".")):
        for rx, secs in UNIT_RES:
            if rx.match(cand):
                return secs
    return None


def parse_relative(text: str, now: float | None = None) -> float | None:
    """'Įkelta prieš 19 val.' -> unix timestamp 19 hours ago. If several
    'uploaded ...' phrases appear, the first one on the page (this item's own
    details, above the seller's other listings) wins."""
    now = now or time.time()
    best: tuple[int, float] | None = None
    m = JUST_NOW.search(text)
    if m:
        best = (m.start(), now)
    for rx in REL_PATTERNS:
        for m in rx.finditer(text):
            if best and m.start() >= best[0]:
                break
            secs = _unit_seconds(m.group(2))
            if secs:
                n = m.group(1)
                best = (m.start(), now - (int(n) if n and n.isdigit() else 1) * secs)
                break
    return best[1] if best else None


def _visible_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", page)
    return " ".join(htmllib.unescape(re.sub(r"<[^>]+>", " ", page)).split())


def _json_unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def parse_item_page(page: str, now: float | None = None, item_id: int | None = None) -> dict:
    """Return {"uploaded_at", "source", "description", "likes", "views", "sold"}."""
    now = now or time.time()
    # Next.js pages often embed JSON with escaped quotes (\\"key\\":...).
    flat = page.replace('\\"', '"')
    found: list[tuple[float, str]] = []
    for key in TS_KEYS:
        for m in re.finditer(rf'"{key}"\s*:\s*"?(\d{{10}})', flat):
            found.append((float(m.group(1)), key))
    for key in ISO_KEYS:
        for m in re.finditer(rf'"{key}"\s*:\s*"(\d{{4}}-\d\d-\d\dT[0-9:.]+(?:Z|[+-]\d\d:?\d\d)?)"', flat):
            try:
                ts = dt.datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
                found.append((ts, key))
            except ValueError:
                pass
    found = [(ts, k) for ts, k in found if 1_200_000_000 < ts <= now + 300]

    uploaded, source = None, "none"
    if found:
        uploaded, source = min(found)          # earliest = creation, not "updated"
    else:
        rel = parse_relative(_visible_text(page), now) or parse_relative(flat, now)
        if rel is not None:
            uploaded, source = rel, "relative text"

    scope = _item_scope(flat, item_id)

    desc = ""
    m = re.search(r'<meta[^>]+(?:property|name)="og:description"[^>]+content="([^"]*)"', page)
    if not m:
        m = re.search(r'<meta[^>]+content="([^"]*)"[^>]+(?:property|name)="og:description"', page)
    if m:
        desc = htmllib.unescape(m.group(1))
    jm = re.search(r'"description"\s*:\s*"((?:[^"\\]|\\.){10,})"', flat)
    if jm and len(_json_unescape(jm.group(1))) > len(desc):
        desc = _json_unescape(jm.group(1))
    return {"uploaded_at": uploaded, "source": source, "description": desc.strip(),
            "likes": _first_int(scope, ("favourite_count", "favorite_count", "favourites_count",
                                        "favouriteCount", "favoriteCount", "favouritesCount")),
            "views": _first_int(scope, ("view_count", "views_count", "viewCount", "viewsCount")),
            "sold": _is_sold(scope),
            "gender": _gender(page, flat),
            "colour": _colour(scope, page)}


_WOMEN_ROOTS = ("moterims", "women", "kobiety", "damen", "femmes", "naiset", "mujer", "donna",
                "dames", "sievietēm", "naised")
_MEN_ROOTS = ("vyrams", "men", "mężczyźni", "herren", "hommes", "miehet", "hombre", "uomo",
              "heren", "vīriešiem", "mehed")


def _gender(page: str, flat: str) -> str | None:
    """From the item's breadcrumb (Home > Moterims > Drabužiai > ...)."""
    names: list[str] = []
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page):
        if "BreadcrumbList" in block:
            names += re.findall(r'"name"\s*:\s*"([^"]+)"', block)
    if not names:                                  # breadcrumb in embedded JSON
        m = re.search(r'"breadcrumbs?"\s*:\s*\[(.{0,3000}?)\]', flat, re.S)
        if m:
            names = re.findall(r'"title"\s*:\s*"([^"]+)"', m.group(1)) or \
                    re.findall(r'"name"\s*:\s*"([^"]+)"', m.group(1))
    for n in names[:4]:                            # top levels only
        n = _json_unescape(n).strip().lower()
        if n in _WOMEN_ROOTS:
            return "women"
        if n in _MEN_ROOTS:
            return "men"
    return None


def _colour(scope: str, page: str) -> str | None:
    m = re.search(r'"color1"\s*:\s*"([^"]+)"', scope) or re.search(r'"colou?rs?"\s*:\s*"([^"]+)"', scope)
    if m:
        return _json_unescape(m.group(1))
    text = _visible_text(page)
    m = re.search(r"(?:Spalva|Colou?r|Kolor|Farbe|Couleur|Väri|Krāsa|Värv)\s*:?\s+"
                  r"([^\W\d_]+(?:\s*,\s*[^\W\d_]+)?)", text)
    return m.group(1) if m else None


def _item_scope(flat: str, item_id: int | None, radius: int = 4000) -> str:
    """The page also embeds 'similar items' with their own like counts. Only
    look at text around this item's own id, if we know it."""
    if not item_id:
        return flat
    m = re.search(rf'"id"\s*:\s*"?{item_id}\b', flat)
    if not m:
        return flat
    return flat[max(0, m.start() - radius): m.end() + radius]


def debug_matches(page: str, limit: int = 25) -> list[str]:
    """Snippets around anything that looks like likes/views/sold data, to find
    the right keys when Vinted changes its page."""
    flat = page.replace('\\"', '"')
    out, seen = [], set()
    rx = re.compile(r'.{0,50}(?:favou?rit\w*|view_?count|viewCount|"likes?"|is_closed|is_sold|'
                    r'is_reserved|closing|status_code|item_status).{0,70}', re.I)
    for m in rx.finditer(flat):
        snip = " ".join(m.group(0).split())
        if snip not in seen:
            seen.add(snip); out.append(snip)
        if len(out) >= limit:
            break
    return out


def _first_int(flat: str, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        m = re.search(rf'"{key}"\s*:\s*(\d+)', flat)
        if m:
            return int(m.group(1))
    return None


def _is_sold(flat: str) -> bool:
    """True if the page's data marks the item as sold/closed."""
    if re.search(r'"(?:is_closed|is_sold|is_hidden)"\s*:\s*true', flat):
        return True
    return bool(re.search(r'"(?:item_closing_action|closing_action|status_code)"\s*:\s*"sold"', flat))
