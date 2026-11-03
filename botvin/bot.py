"""The polling loop: fetch newest listings for every feed, score, alert."""

from __future__ import annotations

import json
import logging
import random
import time

from . import learning
from .models import Item
from .notify import Notifier
from .pricing import PriceModel, letter_size
from .scorer import Thresholds, Verdict, evaluate, measure_demand
from .storage import Store
from .vinted import VintedClient, VintedError

log = logging.getLogger(__name__)

# Re-open a promising item's page this many minutes after upload, to see how
# many likes it is collecting and whether it sold.
CHECK_AT_MINUTES = [5, 10, 15, 25, 40, 60]

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
        self.model = model or PriceModel(brand_overrides=cfg.get("brands"),
                                         filters=cfg.get("filters"))
        self.t = Thresholds(**(cfg.get("thresholds") or {}))
        self.feeds: list[dict] = list(cfg.get("feeds") or [{"name": "everything", "search_text": ""}])
        if cfg.get("auto_brand_feeds", True):
            self.feeds += self._brand_feeds()
        self.fetch_descriptions = bool(cfg.get("fetch_descriptions", True))
        filters = cfg.get("filters") or {}
        # Vinted catalog ids: 5 = Men, 1904 = Women (1193 = Kids, not searched).
        self.adult_catalogs: list[int] = list(filters.get("adult_catalogs", [5, 1904]) or [])
        self._no_catalog_filter: set = set()
        # Page re-checks slow down the sweep; a couple per sweep keeps up with
        # the schedule without delaying new listings much.
        self.checks_per_sweep = int(cfg.get("watch_checks_per_sweep", 2))
        # Review promising items this long after listing: sold or not?
        self.review_hours = float(cfg.get("review_after_hours", 2))
        self.reviews_per_sweep = int(cfg.get("reviews_per_sweep", 1))
        self.learn = bool(cfg.get("learn_from_outcomes", True))

    def _brand_feeds(self) -> list[dict]:
        """A dedicated search for each brand marked `feed_price_to` in
        brands.yaml (UGG, Carhartt...), unless config already searches it."""
        have = {str(f.get("search_text", "")).lower() for f in self.feeds}
        extra = []
        for name, info in self.model.brands.items():
            cap = info.get("feed_price_to")
            text = (info.get("aliases") or [name])[0]
            if cap and text.lower() not in have and name.lower() not in have:
                extra.append({"name": f"auto-{text}", "search_text": text, "price_to": cap})
        if extra:
            log.info("added searches for fast-selling brands: %s",
                     ", ".join(f"{f['search_text']} (≤{f['price_to']}€)" for f in extra))
        return extra

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
        item.gender = item.gender or track.get("gender") or ""
        item.colour = item.colour or track.get("colour") or ""
        if track.get("uploaded_at") and not item.uploaded_at:
            item.uploaded_at = track["uploaded_at"]

        market = self.store.market(brand, category) if brand else (None, 0)
        demand = self._demand(item, track, brand, category)
        est = self._estimate(item, market)
        v = evaluate(item, est, self.t, demand)

        # Open the item page only for promising items (one extra request each):
        # it tells us the real upload time ("Įkelta prieš 19 val.") and the
        # description, which may reveal damage ("uszkodzony zamek") or a fake.
        promising = est.brand and not est.excluded and v.profit >= self.t.min_profit * 0.5
        if (promising and not first_pass and self.fetch_descriptions
                and track.get("description") is None):
            try:
                page = self.client.item_page(item.url)
            except VintedError as e:
                log.debug("item page failed: %s", e)
                page = {"description": "", "uploaded_at": None, "source": "error"}
            self.store.set_page_info(item.id, page["description"], page["uploaded_at"],
                                     page.get("gender"), page.get("colour"))
            item.description = item.description or page["description"]
            item.gender = item.gender or page.get("gender") or ""
            item.colour = item.colour or page.get("colour") or ""
            if page["uploaded_at"] and not item.uploaded_at:
                item.uploaded_at = page["uploaded_at"]
            track = self.store.tracking(item.id)
            demand = self._demand(item, track, brand, category)
            est = self._estimate(item, market)
            v = evaluate(item, est, self.t, demand)

        if is_new:
            self.store.mark_seen(item, est.brand, est.category)
        if promising and not self.store.was_alerted(item.id):
            self._add_to_watchlist(item, track)
        # A re-sighted item only alerts if demand is what changed (it got hot);
        # otherwise drifting market medians could resurface old listings.
        if v.is_deal and not first_pass and (is_new or v.demand.level == "hot"):
            self.notifier.send(item, est, v, heating_up=not is_new)
            self.store.record_alert(item, est.resale, v.profit)
        elif est.brand and is_new:
            log.debug("skip %s [%s/%s] %.2f€ est %.0f: %s", item.title, est.brand,
                      est.category, item.total_price, est.resale, "; ".join(v.reasons))
        return v if is_new else None

    def _estimate(self, item: Item, market):
        """Price model estimate, adjusted by what similar items actually did."""
        est = self.model.estimate(item, market)
        if not self.learn or est.excluded or not est.brand:
            return est
        got = learning.adjust(self.store, est.brand, est.category, est.gender,
                              letter_size(item.size), self.review_hours)
        if got.total:
            est.resale = round(est.resale * got.factor, 2)
            est.learned_factor = got.factor
            est.fast_seller = got.fast
            est.flags.append(got.note)
        return est

    # -------------------------------------------------------------- outcomes
    def review_outcomes(self, now: float | None = None) -> int:
        """~2 h after listing, check whether each promising item sold."""
        now = now or time.time()
        done = 0
        for row in self.store.due_reviews(now, self.review_hours * 3600, self.reviews_per_sweep):
            item = Item(**json.loads(row["item_json"]))
            # After a sale Vinted either shows "Parduotas" or the page stops
            # opening altogether ("Įvyko klaida"). Either way it's gone from search.
            try:
                page = self.client.item_page(item.url)
            except VintedError:
                page = {"error": True}
            status, how = "unsold", None
            likes = row["fav_last"]
            if page.get("sold") or row.get("sold_at"):
                status, how = "sold", "watch" if row.get("sold_at") else "page"
            else:
                found = self.client.find_item(item.id, item.title, item.price)
                if found:
                    likes = max(likes, found.favourites)
                elif page.get("error"):
                    status, how = "sold", "page error + not in search"
                else:
                    status, how = "sold", "not in search"
            start = row.get("uploaded_at") or row["first_seen"]
            sold_minutes = (row["sold_at"] - start) / 60 if row.get("sold_at") else None

            item.favourites = likes
            item.description = row.get("description") or ""
            est = self.model.estimate(item)
            cost = item.total_price + self.t.shipping_cost
            profit = est.resale - cost
            margin = profit / cost if cost else 0
            watched_min = max((row["last_seen"] - start) / 60, 1)
            level = measure_demand(likes, 0, watched_min if not row["from_startup"] or row.get("uploaded_at")
                                   else None, self.t).level
            self.store.record_outcome(
                item.id, status=status, sold_how=how, sold_minutes=sold_minutes,
                alerted=int(self.store.was_alerted(item.id)), brand=row["brand"],
                category=row["category"], gender=est.gender, size_letter=letter_size(item.size),
                colour=row.get("colour"), price=item.total_price, resale=est.resale,
                profit=round(profit, 2), margin=round(margin, 3),
                margin_band=learning.margin_band(margin), demand_level=level, likes=likes,
                title=item.title, url=item.url)
            log.info("review %-6s %-40.40s %s %s/%s %.0f€ ♥%d", status.upper(), item.title,
                     row["brand"], row["category"], letter_size(item.size) or item.size,
                     item.total_price, likes)
            done += 1
        return done

    # ------------------------------------------------------------ watchlist
    @staticmethod
    def _next_check(start: float, now: float) -> float | None:
        elapsed = (now - start) / 60
        for m in CHECK_AT_MINUTES:
            if m > elapsed + 0.5:
                return start + m * 60
        return None

    def _add_to_watchlist(self, item: Item, track: dict) -> None:
        start = track.get("uploaded_at") or track["first_seen"]
        if track["from_startup"] and not track.get("uploaded_at"):
            return                              # unknown age: rates would be meaningless
        nxt = self._next_check(start, time.time())
        if nxt is None:
            return
        snapshot = {k: getattr(item, k) for k in ("id", "title", "price", "total_price", "currency",
                                                   "brand", "size", "condition", "url", "photo_url",
                                                   "gender", "colour")}
        self.store.watch(item.id, nxt, json.dumps(snapshot))

    def check_watchlist(self, now: float | None = None) -> int:
        """Re-open due items: update likes/views/sold, alert if demand got hot."""
        now = now or time.time()
        done = 0
        for row in self.store.due_checks(now, self.checks_per_sweep):
            if not row.get("item_json"):
                self.store.update_check(row["id"], likes=None, views=None, sold=False, next_check=None)
                continue
            item = Item(**json.loads(row["item_json"]))
            try:
                page = self.client.item_page(item.url)
            except VintedError as e:
                log.debug("watch check failed for %s: %s", item.id, e)
                page = {}
            start = row.get("uploaded_at") or page.get("uploaded_at") or row["first_seen"]
            sold = bool(page.get("sold"))
            likes, views = page.get("likes"), page.get("views")
            if likes is None and not sold:
                # The item page doesn't show counts; search results do. Sold
                # items vanish from search (and their page often errors).
                found = self.client.find_item(item.id, item.title, item.price)
                if found:
                    likes, views = found.favourites, found.views
                else:
                    sold = True
            self.store.update_check(item.id, likes=likes, views=views,
                                    sold=sold, next_check=None if sold else self._next_check(start, now),
                                    now=now)
            done += 1
            age = (now - start) / 60
            if sold:
                log.info("SOLD after ~%.0f min: %s (%s)", age, item.title, item.url)
                continue
            track = self.store.tracking(item.id)
            item.favourites, item.views = track["fav_last"], track["views_last"]
            item.description = track.get("description") or ""
            item.uploaded_at = start
            self._rescore_watched(item, track)
        return done

    def _rescore_watched(self, item: Item, track: dict) -> None:
        brand, category = track["brand"], track["category"]
        market = self.store.market(brand, category) if brand else (None, 0)
        demand = self._demand(item, track, brand, category)
        est = self._estimate(item, market)
        v = evaluate(item, est, self.t, demand)
        log.info("watch %-40.40s ♥%-3d %5.1f/h %-6s %s", item.title, demand.likes,
                 demand.likes_per_hour or 0, demand.level, "DEAL" if v.is_deal else "; ".join(v.reasons))
        if demand.level != "hot":
            return
        if not self.store.was_alerted(item.id):
            if v.is_deal:
                self.notifier.send(item, est, v, heating_up=True)
                self.store.record_alert(item, est.resale, v.profit)
        elif not track.get("followup") and not est.excluded:
            self.notifier.send(item, est, v, confirmed=True)
            self.store.set_followup(item.id)

    def _demand(self, item: Item, track: dict, brand: str | None, category: str):
        """Likes/views per hour since upload. Uses the item page's upload time
        when known; otherwise first-seen, which is ~= upload for anything
        listed after the bot started (it polls every ~20 s)."""
        if item.uploaded_at:                   # from the API or the item page
            minutes = item.age_minutes
        elif track["from_startup"]:
            minutes = None                     # was already up when we started
        else:
            minutes = (track["last_seen"] - track["first_seen"]) / 60
        rate, n = self.store.category_demand(brand, category) if brand else (None, 0)
        d = measure_demand(max(item.favourites, track["fav_last"]),
                           max(item.views, track["views_last"]), minutes, self.t, rate, n)
        if brand:
            d.sold, d.watched = self.store.sell_through(brand, category)
        return d

    def poll_feed(self, feed: dict, *, first_pass: bool = False) -> int:
        params = {k: v for k, v in feed.items() if k in FEED_KEYS}
        # Search only the Men + Women sections unless the feed picks its own
        # catalogs, so the flood of kids' listings never even comes back.
        injected = False
        if (self.adult_catalogs and not params.get("catalog_ids")
                and feed.get("name") not in self._no_catalog_filter):
            params["catalog_ids"] = self.adult_catalogs
            injected = True
        items = self.client.search(**params)
        if injected and not items:
            # Empty might just mean a quiet search -- or wrong catalog ids.
            params.pop("catalog_ids")
            unfiltered = self.client.search(**params)
            if unfiltered:
                log.warning("feed %s returns nothing when limited to catalogs %s but does "
                            "without; dropping the catalog filter for it",
                            feed.get("name"), self.adult_catalogs)
                self._no_catalog_filter.add(feed.get("name"))
                items = unfiltered
        new = sum(1 for it in items if self.process(it, first_pass=first_pass) is not None)
        self.store.commit()
        log.info("feed %-20s %3d items, %3d new", feed.get("name", "?"), len(items), new)
        return new

    def run(self, once: bool = False) -> None:
        # Good deals sell within 2-30 minutes, so get back to each feed fast.
        # Requests are still spaced by min_request_interval, so this doesn't
        # hammer Vinted any harder; it just removes idle time.
        interval = float(self.cfg.get("poll_interval", 5))
        # On the very first sweep we only record what's already listed so a
        # restart doesn't spam you with old items -- unless configured otherwise.
        first = not self.cfg.get("alert_on_startup", False)
        while True:
            started = time.time()
            for feed in self.feeds:
                try:
                    self.poll_feed(feed, first_pass=first)
                except VintedError as e:
                    log.error("feed %s failed: %s", feed.get("name"), e)
            try:
                self.check_watchlist()
            except VintedError as e:
                log.error("watchlist failed: %s", e)
            try:
                self.review_outcomes()
            except VintedError as e:
                log.error("outcome review failed: %s", e)
            first = False
            log.info("sweep took %.0fs (each search re-checked every ~%.0fs)",
                     time.time() - started, time.time() - started + interval)
            if once:
                return
            time.sleep(interval * random.uniform(0.8, 1.2))
