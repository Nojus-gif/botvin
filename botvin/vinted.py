"""Minimal client for Vinted's web JSON API (the same endpoints the website uses).

Vinted has no public API. Since September 2026 catalogue search lives on a
dedicated host, ``https://api.vinted.<tld>/svc-catalogue/items``. The old
``www.vinted.<tld>/api/v2/catalog/items`` answers with an HTML 404.

Auth is anonymous: a request to the www homepage sets the ``access_token_web``
cookie (sent as ``Authorization: Bearer``) and an anon id (cookie ``anon_id``
and/or ``X-Anon-Id`` response header, sent back as ``x-anon-id``).
If ``curl_cffi`` is installed we impersonate Chrome's TLS fingerprint, which is
what keeps Vinted's bot protection from returning 403s.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from .itempage import parse_item_page
from .models import Item

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on environment
    from curl_cffi import requests as http
    _IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http  # type: ignore
    _IMPERSONATE = {}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

SEARCH_PATH = "/svc-catalogue/items"
LEGACY_SEARCH_PATH = "/api/v2/catalog/items"

# Old filter name -> new name. The old *_ids names are silently ignored by
# svc-catalogue (you'd get unfiltered results), so always send the new ones.
FILTER_NAMES = {
    "catalog_ids": "attribute_ids[catalog]",
    "brand_ids": "attribute_ids[brand]",
    "size_ids": "attribute_ids[size]",
    "status_ids": "attribute_ids[status]",
    "color_ids": "attribute_ids[color]",
    "material_ids": "attribute_ids[material]",
}


class VintedError(RuntimeError):
    pass


def build_params(search_text: str = "", *, per_page: int = 96, order: str = "newest_first",
                 price_from: float | None = None, price_to: float | None = None,
                 legacy: bool = False, **filters: list[int] | None) -> dict[str, Any]:
    params: dict[str, Any] = {"page": 1, "per_page": per_page, "order": order,
                              "search_text": search_text,
                              "price_from": price_from, "price_to": price_to}
    for key, val in filters.items():
        if key not in FILTER_NAMES:
            raise TypeError(f"unknown filter {key!r}")
        if val:
            params[key if legacy else FILTER_NAMES[key]] = ",".join(str(v) for v in val)
    # svc-catalogue answers 400 to blank parameters.
    return {k: v for k, v in params.items() if v not in ("", None)}


class VintedClient:
    def __init__(self, domain: str = "vinted.lt", min_interval: float = 2.0,
                 locale: str = "lt-LT,lt;q=0.9,en;q=0.8"):
        self.base = f"https://www.{domain}"
        self.api = f"https://api.{domain}"
        self.min_interval = min_interval
        self._last = 0.0
        self.session = http.Session(**_IMPERSONATE)
        self.session.headers.update({
            "User-Agent": UA,
            "Accept-Language": locale,
        })
        self.token: str | None = None
        self.anon_id: str | None = None
        self.last_status: int | None = None
        self.max_per_page = 96

    # ------------------------------------------------------------------ infra
    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))
        self._last = time.time()

    def _cookie(self, name: str) -> str | None:
        try:
            return self.session.cookies.get(name)
        except Exception:  # curl_cffi raises on duplicate names across domains
            for c in getattr(self.session.cookies, "jar", self.session.cookies):
                if getattr(c, "name", None) == name:
                    return c.value
        return None

    def refresh_session(self) -> None:
        self._throttle()
        self.session.cookies.clear()
        r = self.session.get(self.base + "/", timeout=20,
                             headers={"Accept": "text/html,application/xhtml+xml"})
        if r.status_code >= 400:
            raise VintedError(f"session bootstrap failed: HTTP {r.status_code}")
        self.token = self._cookie("access_token_web")
        self.anon_id = r.headers.get("x-anon-id") or self._cookie("anon_id")
        log.info("Vinted session refreshed (%s) token=%s anon_id=%s", self.base,
                 "yes" if self.token else "NO", "yes" if self.anon_id else "no")

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json, text/plain, */*",
             "Origin": self.base, "Referer": self.base + "/"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.anon_id:
            h["x-anon-id"] = self.anon_id
        return h

    def _get(self, url: str, params: dict | None = None, retries: int = 4) -> dict:
        if self.token is None:
            self.refresh_session()
        delay = 5.0
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.session.get(url, params=params, headers=self._headers(), timeout=20)
            except Exception as e:  # network hiccup
                log.warning("request error %s (attempt %d)", e, attempt + 1)
                time.sleep(delay); delay *= 2
                continue
            self.last_status = r.status_code
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise VintedError(f"GET {url} returned non-JSON: {r.text[:120]!r}")
            if r.status_code in (401, 403):
                log.warning("HTTP %s -> refreshing session", r.status_code)
                time.sleep(delay); delay *= 2
                self.refresh_session()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("HTTP %s -> backing off %.0fs", r.status_code, delay)
                time.sleep(delay); delay *= 2
                continue
            body = " ".join(r.text[:300].split())
            raise VintedError(f"GET {url} -> HTTP {r.status_code}: {body[:160]}")
        raise VintedError(f"GET {url} failed after {retries} attempts (last HTTP {self.last_status})")

    # -------------------------------------------------------------------- api
    def search(self, search_text: str = "", *, per_page: int = 96, order: str = "newest_first",
               price_from: float | None = None, price_to: float | None = None,
               **filters: list[int] | None) -> list[Item]:
        per_page = min(per_page, self.max_per_page)
        params = build_params(search_text, per_page=per_page, order=order,
                              price_from=price_from, price_to=price_to, **filters)
        try:
            data = self._get(self.api + SEARCH_PATH, params)
        except VintedError:
            # Some API versions reject large pages with a 400; fall back to 20.
            if self.last_status != 400 or per_page <= 20:
                raise
            log.warning("HTTP 400 with per_page=%d, retrying with 20", per_page)
            self.max_per_page = 20
            params["per_page"] = 20
            data = self._get(self.api + SEARCH_PATH, params)
        items = []
        for raw in data.get("items", []):
            try:
                items.append(Item.from_api(raw, self.base))
            except (KeyError, TypeError, ValueError) as e:
                log.debug("skipping unparsable item: %s", e)
        return items

    def item_page(self, url: str) -> dict:
        """Fetch an item's web page and extract upload time + description.
        Returns {"uploaded_at": ts|None, "source": str, "description": str}."""
        self._throttle()
        r = self.session.get(url, timeout=20, headers={"Accept": "text/html,application/xhtml+xml",
                                                       "Referer": self.base + "/"})
        self.last_status = r.status_code
        if r.status_code != 200:
            raise VintedError(f"item page {url} -> HTTP {r.status_code}")
        return parse_item_page(r.text)

    def probe(self, search_text: str = "columbia") -> list[str]:
        """Diagnostics: try every known endpoint and report what happens."""
        out = []
        try:
            self.refresh_session()
            out.append(f"homepage: ok, token={'yes' if self.token else 'NO'}, "
                       f"anon_id={'yes' if self.anon_id else 'no'}")
        except VintedError as e:
            out.append(f"homepage: {e}")
        try:
            adult = self.search("jacket", catalog_ids=[5, 1904], per_page=20)
            kids = self.search("jacket", catalog_ids=[1193], per_page=20)
            out.append(f"catalog filter: men+women 'jacket' -> {len(adult)} items "
                       f"(sizes: {', '.join(sorted({i.size for i in adult if i.size})[:10])}); "
                       f"kids -> {len(kids)} items "
                       f"(sizes: {', '.join(sorted({i.size for i in kids if i.size})[:6])})")
        except VintedError as e:
            out.append(f"catalog filter: {e}")
        for label, url, legacy in (("svc-catalogue", self.api + SEARCH_PATH, False),
                                   ("legacy api/v2", self.base + LEGACY_SEARCH_PATH, True)):
            params = build_params(search_text, per_page=5, legacy=legacy)
            try:
                data = self._get(url, params, retries=1)
                items = data.get("items") or []
                sample = items[0] if items else {}
                out.append(f"{label}: OK, {len(items)} items; first item keys: {sorted(sample)[:25]}")
                for raw in items[:3]:
                    it = Item.from_api(raw, self.base)
                    out.append(f"   item_box={raw.get('item_box')!r}")
                    out.append(f"   -> title={it.title!r} brand={it.brand!r} size={it.size!r} "
                               f"condition={it.condition} total={it.total_price} likes={it.favourites}")
                if items and not legacy:
                    it = Item.from_api(items[0], self.base)
                    try:
                        page = self.item_page(it.url)
                        age = (f"{(time.time() - page['uploaded_at']) / 60:.0f} min ago"
                               if page["uploaded_at"] else "NOT FOUND")
                        out.append(f"item page {it.url}: uploaded {age} (via {page['source']}); "
                                   f"description: {page['description'][:80]!r}")
                    except VintedError as e:
                        out.append(f"item page: {e}")
            except VintedError as e:
                out.append(f"{label}: {e}")
        return out
