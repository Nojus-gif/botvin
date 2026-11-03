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
]
UNIT_RES = [(re.compile(rf"^(?:{p})$", re.I), secs) for p, secs in UNITS]

# Phrases that mark the relative upload time, around "N unit".
REL_PATTERNS = [
    re.compile(r"(?:įkelta|ikelta)\s+prieš\s+(\d+)\s+([^\s<.,]+\.?)", re.I),           # LT
    re.compile(r"(?:uploaded|added|posted)\s+(\d+)\s+([a-z]+)\s+ago", re.I),            # EN
    re.compile(r"(?:dodan[eoy]|dodano)\s+(\d+)\s+([^\s<.,]+)\s+temu", re.I),            # PL
    re.compile(r"(\d+)\s+([^\s<.,]+)\s+temu", re.I),                                    # PL short
    re.compile(r"(?:hochgeladen|eingestellt)\s+vor\s+(\d+)\s+([^\s<.,]+)", re.I),       # DE
    re.compile(r"(?:ajouté|publié)\s+il\s+y\s+a\s+(\d+)\s+([^\s<.,]+)", re.I),          # FR
    re.compile(r"(?:subido|publicado)\s+hace\s+(\d+)\s+([^\s<.,]+)", re.I),             # ES
    re.compile(r"(?:caricato|pubblicato)\s+(\d+)\s+([^\s<.,]+)\s+fa", re.I),            # IT
    re.compile(r"(?:lisätty|ladattu)\s+(\d+)\s+([^\s<.,]+)\s+sitten", re.I),            # FI
    re.compile(r"(\d+)\s+([^\s<.,]+)\s+(?:sitten|ago)", re.I),
]


def _unit_seconds(word: str) -> int | None:
    w = word.strip().rstrip(".").lower()
    for rx, secs in UNIT_RES:
        if rx.match(w) or rx.match(w + "."):
            return secs
    return None


def parse_relative(text: str, now: float | None = None) -> float | None:
    """'Įkelta prieš 19 val.' -> unix timestamp 19 hours ago."""
    now = now or time.time()
    for rx in REL_PATTERNS:
        for m in rx.finditer(text):
            secs = _unit_seconds(m.group(2))
            if secs:
                return now - int(m.group(1)) * secs
    low = text.lower()
    for phrase in ("ką tik", "just now", "przed chwilą", "gerade eben", "à l'instant", "juuri nyt"):
        if phrase in low:
            return now
    return None


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
            "likes": _first_int(scope, ("favourite_count", "favorite_count", "favourites_count")),
            "views": _first_int(scope, ("view_count", "views_count")),
            "sold": _is_sold(scope)}


def _item_scope(flat: str, item_id: int | None, radius: int = 4000) -> str:
    """The page also embeds 'similar items' with their own like counts. Only
    look at text around this item's own id, if we know it."""
    if not item_id:
        return flat
    m = re.search(rf'"id"\s*:\s*"?{item_id}\b', flat)
    if not m:
        return flat
    return flat[max(0, m.start() - radius): m.end() + radius]


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
