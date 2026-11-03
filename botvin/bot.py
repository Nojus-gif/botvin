"""The polling loop: fetch newest listings for every feed, score, alert."""

from __future__ import annotations

import logging
import random
import time

from .models import Item
from .notify import Notifier
from .pricing import PriceModel
from .scorer import Thresholds, Verdict, evaluate, measure_demand
from .storage import Store
from .vinted import VintedClient, VintedError

log = logging.getLogger(__name__)

FEED_KEYS = {"search_text", "catalog_ids", "brand_ids", "size_ids", "status_ids",
             "price_from", "price_to", "per_page"}


class Bot:
    def __init__(self, cfg: dict, client: VintedClient | None = None,
                 store: Store | None = None, notifier: Notifier | None = None,
                 model: PriceModel | None = None):
        self.cfg = cfg
        self.client = client or VintedClient(
            domain=cfg.get("domain", "vinted.lt"),
            min_interval=float(cfg.get("min_request_interval", 2.0)),
        )
        self.store = store or Store(cfg.get("database", "botvin.db"))
        self.notifier = notifier or Notifier(cfg.get("notify") or {})
        self.model = model or PriceModel(brand_overrides=cfg.get("brands"))
        self.t = Thresholds(**(cfg.get("thresholds") or {}))
        self.feeds: list[dict] = cfg.get("feeds") or [{"name": "everything", "search_text": ""}]
        self.fetch_descriptions = bool(cfg.get("fetch_descriptions", True))

    # ------------------------------------------------------------------ core
    def process(self, item: Item, *, first_pass: bool = False) -> Verdict | None:
        """Score an item. Called every time it shows up in a feed, so likes
        and views are re-measured each sweep. Returns the verdict, or None if
        the item had already been seen and nothing about it was re-scored."""
        is_new = not self.store.is_seen(item.id)
        brand, _ = self.model.match_brand(item)
        category = self.model.detect_category(item.title)
        if not brand and not is_new:
            return None                        # unbranded: nothing to re-score

        track = self.store.track(item, brand, category, from_startup=first_pass and is_new)
        if self.store.was_alerted(item.id):
            return None                        # already sent; don't spam
        if track.get("description"):
            item.description = track["description"]

        market = self.store.market(brand, category) if brand else (None, 0)
        demand = self._demand(item, track, brand, category)
        est = self.model.estimate(item, market)
        v = evaluate(item, est, self.t, demand)

        # Only items that already look like deals are worth an extra request:
        # the description may reveal damage ("uszkodzony zamek") or a fake.
        if (v.is_deal and not first_pass and self.fetch_descriptions
                and track.get("description") is None):
            try:
                item.description = self.client.description(item.id)
            except VintedError as e:
                log.debug("description fetch failed: %s", e)
            self.store.set_description(item.id, item.description or "")
            if item.description:
                est = self.model.estimate(item, market)
                v = evaluate(item, est, self.t, demand)

        if is_new:
            self.store.mark_seen(item, est.brand, est.category)
        # A re-sighted item only alerts if demand is what changed (it got hot);
        # otherwise drifting market medians could resurface old listings.
        if v.is_deal and not first_pass and (is_new or v.demand.level == "hot"):
            self.notifier.send(item, est, v, heating_up=not is_new)
            self.store.record_alert(item, est.resale, v.profit)
        elif est.brand and is_new:
            log.debug("skip %s [%s/%s] %.2f€ est %.0f: %s", item.title, est.brand,
                      est.category, item.total_price, est.resale, "; ".join(v.reasons))
        return v if is_new else None

    def _demand(self, item: Item, track: dict, brand: str | None, category: str):
        """Likes/views per hour since we first saw the item. The bot polls every
        ~20 s, so for anything listed after it started, first-seen ~= upload."""
        if item.uploaded_at:                   # API still sent a timestamp
            minutes = item.age_minutes
        elif track["from_startup"]:
            minutes = None                     # was already up when we started
        else:
            minutes = (track["last_seen"] - track["first_seen"]) / 60
        rate, n = self.store.category_demand(brand, category) if brand else (None, 0)
        return measure_demand(max(item.favourites, track["fav_last"]),
                              max(item.views, track["views_last"]), minutes, self.t, rate, n)

    def poll_feed(self, feed: dict, *, first_pass: bool = False) -> int:
        params = {k: v for k, v in feed.items() if k in FEED_KEYS}
        items = self.client.search(**params)
        new = sum(1 for it in items if self.process(it, first_pass=first_pass) is not None)
        self.store.commit()
        log.info("feed %-20s %3d items, %3d new", feed.get("name", "?"), len(items), new)
        return new

    def run(self, once: bool = False) -> None:
        interval = float(self.cfg.get("poll_interval", 20))
        # On the very first sweep we only record what's already listed so a
        # restart doesn't spam you with old items -- unless configured otherwise.
        first = not self.cfg.get("alert_on_startup", False)
        while True:
            for feed in self.feeds:
                try:
                    self.poll_feed(feed, first_pass=first)
                except VintedError as e:
                    log.error("feed %s failed: %s", feed.get("name"), e)
            first = False
            if once:
                return
            time.sleep(interval * random.uniform(0.8, 1.2))
