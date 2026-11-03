"""SQLite store: which items we've seen, observed prices (for market medians)
and which items we've alerted on."""

from __future__ import annotations

import sqlite3
import statistics
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    id INTEGER PRIMARY KEY,
    first_seen REAL NOT NULL,
    brand TEXT,
    category TEXT,
    size TEXT,
    condition TEXT,
    price REAL,
    title TEXT,
    url TEXT
);
CREATE INDEX IF NOT EXISTS seen_brand_cat ON seen(brand, category, first_seen);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    sent_at REAL NOT NULL,
    total_price REAL,
    resale REAL,
    profit REAL,
    title TEXT,
    url TEXT
);
"""


class Store:
    def __init__(self, path: str | Path = "botvin.db"):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)

    def is_seen(self, item_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM seen WHERE id=?", (item_id,)).fetchone() is not None

    def mark_seen(self, item, brand: str | None, category: str) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen (id, first_seen, brand, category, size, condition, price, title, url)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (item.id, time.time(), brand, category, item.size, item.condition, item.total_price,
             item.title, item.url),
        )
        self.db.commit()

    def market(self, brand: str, category: str, days: int = 30) -> tuple[float | None, int]:
        """Median asking price of comparable items seen in the last `days`."""
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT price FROM seen WHERE brand=? AND category=? AND first_seen>=? AND price>0",
            (brand, category, since),
        ).fetchall()
        prices = [r[0] for r in rows]
        if not prices:
            return None, 0
        return statistics.median(prices), len(prices)

    def record_alert(self, item, resale: float, profit: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO alerts (id, sent_at, total_price, resale, profit, title, url)"
            " VALUES (?,?,?,?,?,?,?)",
            (item.id, time.time(), item.total_price, resale, profit, item.title, item.url),
        )
        self.db.commit()

    def was_alerted(self, item_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE id=?", (item_id,)).fetchone() is not None
