"""Learn real demand from what actually sold.

Every promising item (and every alert) is reviewed once, ~2 hours after it
was listed: sold, still listed, or removed. From those outcomes we learn which
kinds of items sell fast and adjust future estimates:

  * brand + type        (e.g. Carhartt jackets: 9/10 sold within 2 h)
  * brand
  * type
  * gender + size       (e.g. women's L: 2/9 sold)
  * price vs. estimate  (are the bot's "huge margin" picks really selling?)

Counts are smoothed toward the overall sell rate, so 2 lucky sales don't swing
anything; a group needs a few outcomes before it matters.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_TOTAL = 10       # outcomes overall before learning kicks in
MIN_GROUP = 3        # outcomes in a group before it's used
PRIOR_STRENGTH = 5   # how many "virtual" average outcomes each group starts with
FACTOR_RANGE = (0.7, 1.3)


@dataclass
class Learned:
    factor: float = 1.0          # multiplies the resale estimate
    rate: float | None = None    # smoothed sell rate of the group used
    sold: int = 0
    total: int = 0
    group: str = ""              # human label, e.g. "Carhartt jacket"
    note: str = ""               # for the alert

    @property
    def fast(self) -> bool:
        """This kind of item reliably sells within the review window."""
        return self.total >= 5 and self.rate is not None and self.rate >= 0.70


def margin_band(margin: float) -> str:
    if margin < 1.0:
        return "<100%"
    if margin < 2.0:
        return "100-200%"
    return ">200%"


def groups_for(brand: str | None, category: str, gender: str | None, size: str | None) -> list[tuple[str, dict, str]]:
    """Most specific first: (label, filter on outcome columns, kind)."""
    out = []
    if brand:
        out.append((f"{brand} {category}", {"brand": brand, "category": category}, "brand+type"))
        out.append((brand, {"brand": brand}, "brand"))
    out.append((category, {"category": category}, "type"))
    if gender and size:
        out.append((f"{gender}'s {size.upper()}", {"gender": gender, "size_letter": size}, "gender+size"))
    return out


def adjust(store, brand: str | None, category: str, gender: str | None,
           size_letter: str | None, window_hours: float) -> Learned:
    overall_sold, overall_total = store.outcome_counts({})
    if overall_total < MIN_TOTAL:
        return Learned()
    p0 = overall_sold / overall_total
    p0 = min(max(p0, 0.05), 0.95)
    for label, where, _kind in groups_for(brand, category, gender, size_letter):
        sold, total = store.outcome_counts(where)
        if total < MIN_GROUP:
            continue
        rate = (sold + PRIOR_STRENGTH * p0) / (total + PRIOR_STRENGTH)
        factor = min(max((rate / p0) ** 0.5, FACTOR_RANGE[0]), FACTOR_RANGE[1])
        hours = f"{window_hours:g} h"
        note = f"learned: {label} {sold}/{total} sold within {hours}"
        if abs(factor - 1) >= 0.03:
            note += f" ×{factor:.2f}"
        return Learned(round(factor, 3), rate, sold, total, label, note)
    return Learned()


def report(store, window_hours: float) -> str:
    """Text summary of what sells, for `python -m botvin report`."""
    sold, total = store.outcome_counts({})
    lines = [f"Reviewed {total} items {window_hours:g} h after listing: {sold} sold "
             f"({(sold / total * 100) if total else 0:.0f}%)."]
    if not total:
        lines.append("No outcomes yet. Leave the bot running for a few hours.")
        return "\n".join(lines)

    def table(title: str, column: str, min_n: int = 2) -> None:
        rows = store.outcome_breakdown(column)
        rows = [r for r in rows if r[2] >= min_n]
        if not rows:
            return
        lines.append("")
        lines.append(title)
        for key, s, n, avg_minutes in sorted(rows, key=lambda r: (-r[1] / r[2], -r[2]))[:15]:
            speed = f", sold ones went in ~{avg_minutes:.0f} min" if avg_minutes else ""
            lines.append(f"  {key or '?':28.28} {s:3d}/{n:<3d} sold  {s / n * 100:4.0f}%{speed}")

    table("By brand + type:", "brand || ' ' || category")
    table("By brand:", "brand")
    table("By type:", "category")
    table("By gender + size:", "gender || '''s ' || upper(size_letter)")
    table("By the bot's estimated margin (is it right about big margins?):", "margin_band")
    table("By demand level at alert time:", "demand_level")
    table("How sales were detected:", "COALESCE(sold_how, 'still listed')", min_n=1)
    return "\n".join(lines)


def phone_summary(store, window_hours: float, since: float) -> tuple[str, str] | None:
    """Short push-notification version of the report. None if nothing new."""
    new = store.outcomes_since(since)
    if not new:
        return None
    sold, total = store.outcome_counts({})
    new_sold = sum(1 for r in new if r[0] == "sold")
    title = (f"📊 Learned: {new_sold}/{len(new)} new deals sold within {window_hours:g} h · "
             f"{sold}/{total} overall ({sold / total * 100:.0f}%)")

    lines: list[str] = []
    groups = [r for r in store.outcome_breakdown("brand || ' ' || category") if r[2] >= MIN_GROUP]
    groups += [r for r in store.outcome_breakdown("gender || '''s ' || upper(size_letter)")
               if r[0] and r[2] >= MIN_GROUP]
    p0 = sold / total if total else 0.5
    fast = sorted((g for g in groups if g[1] / g[2] >= max(0.7, p0 + 0.15)),
                  key=lambda g: (-g[1] / g[2], -g[2]))[:4]
    slow = sorted((g for g in groups if g[1] / g[2] <= min(0.35, p0 - 0.15)),
                  key=lambda g: (g[1] / g[2], -g[2]))[:4]
    if fast:
        lines.append("Sells fast (boosted):")
        lines += [f"  ✅ {k} {s}/{n}" + (f", ~{m:.0f} min" if m else "") for k, s, n, m in fast]
    if slow:
        lines.append("Doesn't sell (fewer alerts):")
        lines += [f"  ❌ {k} {s}/{n}" for k, s, n, m in slow]
    bands = {k: (s, n) for k, s, n, _ in store.outcome_breakdown("margin_band")}
    if bands.get(">200%", (0, 0))[1] >= MIN_GROUP:
        s, n = bands[">200%"]
        lines.append(f"Bot's biggest-margin picks (>200%): {s}/{n} sold")
    if total < MIN_TOTAL:
        lines.append(f"Still collecting: learning starts at {MIN_TOTAL} reviewed items ({total} so far).")
    lines.append("")
    lines.append("Latest:")
    for status, t, price, *_ in new[-6:]:
        lines.append(f"  {'✅ sold' if status == 'sold' else '⏳ unsold'} · {price:.0f}€ · {t[:40]}")
    return title, "\n".join(lines)
