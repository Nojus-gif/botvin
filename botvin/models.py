"""Data model for a Vinted listing, parsed from the catalog API JSON."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Vinted condition ids (stable across locales; the `status` text is localized).
CONDITION_BY_ID = {
    6: "new_with_tags",
    1: "new_without_tags",
    2: "very_good",
    3: "good",
    4: "satisfactory",
}

# Localized condition labels -> canonical key, used when no status_id is sent.
CONDITION_BY_LABEL = {
    # EN
    "new with tags": "new_with_tags", "new without tags": "new_without_tags",
    "very good": "very_good", "good": "good", "satisfactory": "satisfactory",
    # LT
    "naujas su etiketėmis": "new_with_tags", "naujas be etikečių": "new_without_tags",
    "labai gera": "very_good", "gera": "good", "patenkinama": "satisfactory",
    # PL
    "nowy z metką": "new_with_tags", "nowy bez metki": "new_without_tags",
    "bardzo dobry": "very_good", "dobry": "good", "zadowalający": "satisfactory",
    # DE
    "neu mit etikett": "new_with_tags", "neu ohne etikett": "new_without_tags",
    "sehr gut": "very_good", "gut": "good", "zufriedenstellend": "satisfactory",
    # FR
    "neuf avec étiquette": "new_with_tags", "neuf sans étiquette": "new_without_tags",
    "très bon état": "very_good", "bon état": "good", "satisfaisant": "satisfactory",
}


def _money(value: Any) -> float | None:
    """Vinted sends money either as "12.60" or {"amount": "12.60", ...}."""
    if value is None:
        return None
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


@dataclass
class Item:
    id: int
    title: str
    price: float                     # seller's price
    total_price: float               # price incl. buyer protection fee
    currency: str
    brand: str
    size: str
    condition: str                   # canonical key, or "unknown"
    url: str
    photo_url: str | None = None
    favourites: int = 0
    views: int = 0
    uploaded_at: float | None = None  # unix ts, from the photo timestamp
    seller: str = ""
    description: str = ""
    gender: str = ""                 # "women" / "men" from the item page, if known
    colour: str = ""                 # colour name from the item page, if known
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def age_minutes(self) -> float | None:
        if not self.uploaded_at:
            return None
        return max(0.0, (time.time() - self.uploaded_at) / 60)

    @classmethod
    def from_api(cls, d: dict, base_url: str = "https://www.vinted.lt") -> "Item":
        price = _money(d.get("price")) or 0.0
        total = _money(d.get("total_item_price"))
        if total is None:
            fee = _money(d.get("service_fee")) or 0.0
            total = price + fee
        currency = (
            (d.get("price") or {}).get("currency_code")
            if isinstance(d.get("price"), dict)
            else d.get("currency")
        ) or "EUR"

        status_id = d.get("status_id")
        condition = CONDITION_BY_ID.get(status_id) if status_id is not None else None
        if not condition:
            condition = CONDITION_BY_LABEL.get(str(d.get("status", "")).strip().lower(), "unknown")

        photo = d.get("photo") or {}
        photo_url = photo.get("full_size_url") or photo.get("url")
        uploaded = (photo.get("high_resolution") or {}).get("timestamp")

        # Newer responses may omit brand_title/size_title and only carry
        # item_box: branded -> first_line = brand, second_line = "size · condition".
        box = d.get("item_box") or {}
        brand = d.get("brand_title") or ""
        size = d.get("size_title") or ""
        second = [p.strip() for p in str(box.get("second_line") or "").split("·") if p.strip()]
        if not brand and box.get("first_line") and box.get("first_line") != d.get("title"):
            brand = box["first_line"]
        if not size and len(second) >= 2:
            size = second[0]
        if condition == "unknown" and second:
            condition = CONDITION_BY_LABEL.get(second[-1].lower(), "unknown")

        url = d.get("url") or f"/items/{d['id']}"
        if url.startswith("/"):
            url = base_url.rstrip("/") + url

        return cls(
            id=int(d["id"]),
            title=d.get("title") or "",
            price=price,
            total_price=total,
            currency=currency,
            brand=brand,
            size=size,
            condition=condition,
            url=url,
            photo_url=photo_url,
            favourites=int(d.get("favourite_count") or 0),
            views=int(d.get("view_count") or 0),
            uploaded_at=float(uploaded) if uploaded else None,
            seller=(d.get("user") or {}).get("login", ""),
            description=d.get("description") or "",
            raw=d,
        )
