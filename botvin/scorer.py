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
    max_age_minutes: float = 90.0     # ignore listings older than this
    high_priority_profit: float = 50.0


@dataclass
class Verdict:
    is_deal: bool
    profit: float
    margin: float
    priority: str                     # "high" | "normal" | "low"
    reasons: list[str] = field(default_factory=list)
    hype: float = 0.0                 # favourites per hour since upload


def evaluate(item: Item, est: Estimate, t: Thresholds) -> Verdict:
    cost = item.total_price + t.shipping_cost
    profit = est.resale - cost - t.resell_cost
    margin = profit / cost if cost > 0 else 0.0

    age = item.age_minutes
    hype = (item.favourites / max(age, 5) * 60) if age is not None else 0.0

    reasons: list[str] = []
    ok = True
    if est.excluded:
        ok, reasons = False, [est.excluded]
    else:
        if item.total_price > t.max_price:
            ok = False; reasons.append(f"price {item.total_price:.0f} > max {t.max_price:.0f}")
        if profit < t.min_profit:
            ok = False; reasons.append(f"profit {profit:.0f} < {t.min_profit:.0f}")
        if margin < t.min_margin:
            ok = False; reasons.append(f"margin {margin:.0%} < {t.min_margin:.0%}")
        if est.confidence < t.min_confidence:
            ok = False; reasons.append(f"confidence {est.confidence:.2f} < {t.min_confidence:.2f}")
        if age is not None and age > t.max_age_minutes:
            ok = False; reasons.append(f"listed {age:.0f} min ago")

    if profit >= t.high_priority_profit or margin >= 2.0 or hype >= 6:
        priority = "high"
    elif ok:
        priority = "normal"
    else:
        priority = "low"
    return Verdict(ok, round(profit, 2), round(margin, 3), priority, reasons, round(hype, 1))
