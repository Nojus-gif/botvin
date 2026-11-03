"""Turn a price estimate into a buy/skip decision."""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Item
from .pricing import Estimate


@dataclass
class Thresholds:
    min_profit: float = 20.0          # EUR, after buying + shipping costs
    min_margin: float = 0.6           # profit / total buy price
    min_confidence: float = 0.6
    max_price: float = 400.0          # never alert on items above this total
    shipping_cost: float = 3.5        # what you pay to get it shipped to you
    resell_cost: float = 0.0          # e.g. packaging; Vinted sellers pay no fee
    max_age_minutes: float = 60.0     # older than this and still unsold = not a good deal
    high_priority_profit: float = 50.0
    # Demand (likes per hour since upload). Popular brands get seen by lots of
    # people fast; few likes after a while means it won't resell quickly.
    # Rule of thumb: unsold after an hour with only ~10 likes = weak demand.
    hot_likes_per_hour: float = 20.0
    hot_min_likes: int = 5            # 3 likes in 5 min is not proof of anything
    warm_likes_per_hour: float = 12.0
    cold_likes_per_hour: float = 12.0  # below this after cold_after_minutes -> not a deal
    cold_after_minutes: float = 15.0
    cold_like_view_ratio: float = 0.04  # <4 likes per 100 views (once 60+ views) -> cold
    min_watch_minutes: float = 3.0    # need this long before a rate means anything
    hot_margin_relief: float = 0.25   # hot items may have this much lower margin
    hot_profit_relief: float = 0.25   # ...and this fraction lower min profit


@dataclass
class Demand:
    likes: int = 0
    views: int = 0
    minutes: float | None = None      # how long we've watched it (None = unknown age)
    likes_per_hour: float | None = None
    views_per_hour: float | None = None
    category_likes_per_hour: float | None = None   # median for same brand+category
    category_samples: int = 0
    level: str = "unknown"            # hot | warm | normal | cold | unknown
    sold: int = 0                     # comparable items that sold within an hour...
    watched: int = 0                  # ...out of this many we watched

    @property
    def relative(self) -> float | None:
        """How much more liked than comparable items (2.0 = twice as many likes/h)."""
        if self.likes_per_hour is None or not self.category_likes_per_hour:
            return None
        return self.likes_per_hour / self.category_likes_per_hour

    def describe(self) -> str:
        if self.minutes is None:
            return f"♥ {self.likes}" + (f" · 👁 {self.views}" if self.views else "")
        if self.minutes < 1:
            return f"♥ {self.likes} · just listed"
        parts = [f"♥ {self.likes} in {self.minutes:.0f} min"]
        if self.likes_per_hour is not None:
            parts[0] += f" ({self.likes_per_hour:.0f}/h)"
        if self.views:
            parts.append(f"👁 {self.views}")
        if self.level in ("hot", "warm", "cold"):
            parts.append({"hot": "🔥 HOT", "warm": "warm", "cold": "❄ low demand"}[self.level])
        elif self.level == "normal":
            parts.append("too early to judge demand")
        if self.watched >= 5:
            parts.append(f"{self.sold}/{self.watched} similar sold within 1 h")
        return " · ".join(parts)


def measure_demand(likes: int, views: int, minutes: float | None, t: "Thresholds",
                   category_rate: float | None = None, category_samples: int = 0) -> Demand:
    d = Demand(likes=likes, views=views, minutes=minutes,
               category_likes_per_hour=category_rate, category_samples=category_samples)
    if minutes is None:
        return d
    # Floor the window so 1 like in the first 20 seconds isn't "180 likes/hour".
    hours = max(minutes, t.min_watch_minutes) / 60
    d.likes_per_hour = likes / hours
    d.views_per_hour = views / hours
    # Only absolute likes count. (Comparing with "usual for this brand" made
    # 4 likes/hour look hot when everything else was also unpopular.)
    hot = d.likes_per_hour >= t.hot_likes_per_hour and likes >= t.hot_min_likes
    judged = minutes >= t.cold_after_minutes
    # Seen by lots of people but few of them liked it: weak real interest.
    low_ratio = views >= 60 and likes / views < t.cold_like_view_ratio
    if hot and not low_ratio:
        d.level = "hot"
    elif judged and (d.likes_per_hour < t.cold_likes_per_hour or low_ratio):
        d.level = "cold"
    elif d.likes_per_hour >= t.warm_likes_per_hour and likes >= 2:
        d.level = "warm"
    else:
        d.level = "normal"              # too early to tell
    return d


@dataclass
class Verdict:
    is_deal: bool
    profit: float
    margin: float
    priority: str                     # "high" | "normal" | "low"
    reasons: list[str] = field(default_factory=list)
    hype: float = 0.0                 # favourites per hour since first seen
    demand: Demand = field(default_factory=Demand)


def evaluate(item: Item, est: Estimate, t: Thresholds, demand: Demand | None = None) -> Verdict:
    cost = item.total_price + t.shipping_cost
    profit = est.resale - cost - t.resell_cost
    margin = profit / cost if cost > 0 else 0.0

    age = item.age_minutes
    if demand is None:
        demand = measure_demand(item.favourites, item.views, age, t)
    hype = demand.likes_per_hour or 0.0

    # Strong demand means it will resell fast, so accept a thinner margin.
    min_profit = max(t.min_profit, est.min_profit or 0)
    min_margin = max(t.min_margin, est.min_margin or 0)
    # ...and so do brands that are known to sell within a minute.
    if demand.level == "hot" or est.instant or est.fast_seller:
        min_profit *= 1 - t.hot_profit_relief
        min_margin -= t.hot_margin_relief

    reasons: list[str] = []
    ok = True
    if est.excluded:
        ok, reasons = False, [est.excluded]
    else:
        if item.total_price > t.max_price:
            ok = False; reasons.append(f"price {item.total_price:.0f} > max {t.max_price:.0f}")
        if est.max_buy and item.total_price > est.max_buy:
            ok = False
            reasons.append(f"{est.brand} only worth it at {est.max_buy:.0f}€ or less")
        if profit < min_profit:
            ok = False; reasons.append(f"profit {profit:.0f} < {min_profit:.0f}")
        if margin < min_margin:
            ok = False; reasons.append(f"margin {margin:.0%} < {min_margin:.0%}")
        if demand.level == "cold":
            ok = False
            reasons.append(f"low demand: {demand.likes} likes in {demand.minutes:.0f} min")
        if est.confidence < t.min_confidence:
            ok = False; reasons.append(f"confidence {est.confidence:.2f} < {t.min_confidence:.2f}")
        known_age = age if age is not None else demand.minutes
        if known_age is not None and known_age > t.max_age_minutes:
            ok = False; reasons.append(f"listed {known_age:.0f} min ago")

    if (profit >= t.high_priority_profit or margin >= 2.0 or demand.level == "hot"
            or est.instant or est.fast_seller):
        priority = "high"
    elif ok:
        priority = "normal"
    else:
        priority = "low"
    return Verdict(ok, round(profit, 2), round(margin, 3), priority, reasons, round(hype, 1), demand)
