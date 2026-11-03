"""Minimal client for Vinted's web JSON API (the same endpoints the website uses).

Vinted has no public API. The site sets an anonymous session cookie
(`access_token_web`) on the first page load; the catalog API accepts it.
If `curl_cffi` is installed we impersonate Chrome's TLS fingerprint, which is
what keeps Vinted's bot protection from returning 403s.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Any

from .models import Item

log = logging.getLogger(__name__)

try:  # pragma: no cover - depends on environment
    from curl_cffi import requests as http
    _IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http  # type: ignore
    _IMPERSONATE = {}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


class VintedError(RuntimeError):
    pass


class VintedClient:
    def __init__(self, domain: str = "vinted.lt", min_interval: float = 2.0,
                 locale: str = "lt-LT,lt;q=0.9,en;q=0.8"):
        self.base = f"https://www.{domain}"
        self.min_interval = min_interval
        self._last = 0.0
        self.session = http.Session(**_IMPERSONATE)
        self.session.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": locale,
            "Referer": self.base + "/",
        })
        self._has_session = False

    # ------------------------------------------------------------------ infra
    def _throttle(self) -> None:
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait + random.uniform(0, 0.5))
        self._last = time.time()

    def refresh_session(self) -> None:
        self._throttle()
        self.session.cookies.clear()
        r = self.session.get(self.base + "/", headers={"Accept": "text/html"}, timeout=20)
        if r.status_code >= 400:
            raise VintedError(f"session bootstrap failed: HTTP {r.status_code}")
        self._has_session = True
        log.info("Vinted session refreshed (%s)", self.base)

    def _get(self, path: str, params: dict | None = None, retries: int = 4) -> dict:
        if not self._has_session:
            self.refresh_session()
        delay = 5.0
        for attempt in range(retries):
            self._throttle()
            try:
                r = self.session.get(self.base + path, params=params, timeout=20)
            except Exception as e:  # network hiccup
                log.warning("request error %s (attempt %d)", e, attempt + 1)
                time.sleep(delay); delay *= 2
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code in (401, 403):
                log.warning("HTTP %s -> refreshing session", r.status_code)
                time.sleep(delay); delay *= 2
                self.refresh_session()
                continue
            if r.status_code == 429 or r.status_code >= 500:
                log.warning("HTTP %s -> backing off %.0fs", r.status_code, delay)
                time.sleep(delay); delay *= 2
                continue
            raise VintedError(f"GET {path} -> HTTP {r.status_code}: {r.text[:200]}")
        raise VintedError(f"GET {path} failed after {retries} attempts")

    # -------------------------------------------------------------------- api
    def search(self, search_text: str = "", *, catalog_ids: list[int] | None = None,
               brand_ids: list[int] | None = None, size_ids: list[int] | None = None,
               status_ids: list[int] | None = None, price_from: float | None = None,
               price_to: float | None = None, per_page: int = 96,
               order: str = "newest_first") -> list[Item]:
        params: dict[str, Any] = {"page": 1, "per_page": per_page, "order": order,
                                  "search_text": search_text}
        for key, val in (("catalog_ids", catalog_ids), ("brand_ids", brand_ids),
                         ("size_ids", size_ids), ("status_ids", status_ids)):
            if val:
                params[key] = ",".join(str(v) for v in val)
        if price_from is not None:
            params["price_from"] = price_from
        if price_to is not None:
            params["price_to"] = price_to
        data = self._get("/api/v2/catalog/items", params)
        items = []
        for raw in data.get("items", []):
            try:
                items.append(Item.from_api(raw, self.base))
            except (KeyError, TypeError, ValueError) as e:
                log.debug("skipping unparsable item: %s", e)
        return items

    def description(self, item_id: int) -> str:
        """Best effort: fetch the item description (not included in search results)."""
        for path in (f"/api/v2/items/{item_id}/details", f"/api/v2/items/{item_id}"):
            try:
                data = self._get(path, retries=1)
            except VintedError:
                continue
            node = data.get("item") or data
            desc = node.get("description")
            if desc:
                return desc
        return ""
