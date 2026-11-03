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
-- Demand tracking: every time an item shows up again in a feed we update its
-- like/view counts, so likes-per-hour since it was first seen is known.
CREATE TABLE IF NOT EXISTS tracking (
    id INTEGER PRIMARY KEY,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    fav_first INTEGER NOT NULL,
    fav_last INTEGER NOT NULL,
    views_first INTEGER NOT NULL,
    views_last INTEGER NOT NULL,
    sightings INTEGER NOT NULL DEFAULT 1,
    from_startup INTEGER NOT NULL DEFAULT 0,  -- already listed when bot started: age unknown
    brand TEXT,
    category TEXT,
    description TEXT                          -- NULL = not fetched yet
);
CREATE INDEX IF NOT EXISTS tracking_bc ON tracking(brand, category, first_seen);
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

    # ------------------------------------------------------------- tracking
    def track(self, item, brand: str | None, category: str, *, from_startup: bool,
              now: float | None = None) -> dict:
        """Insert or update the demand snapshot for an item; return the row."""
        now = now or time.time()
        row = self.db.execute("SELECT * FROM tracking WHERE id=?", (item.id,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO tracking (id, first_seen, last_seen, fav_first, fav_last, views_first,"
                " views_last, from_startup, brand, category) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (item.id, now, now, item.favourites, item.favourites, item.views, item.views,
                 int(from_startup), brand, category))
        else:
            self.db.execute(
                "UPDATE tracking SET last_seen=?, fav_last=MAX(fav_last, ?), views_last=MAX(views_last, ?),"
                " sightings=sightings+1 WHERE id=?", (now, item.favourites, item.views, item.id))
        return self.tracking(item.id)   # caller commits (once per feed)

    def commit(self) -> None:
        self.db.commit()

    def tracking(self, item_id: int) -> dict | None:
        cur = self.db.execute("SELECT * FROM tracking WHERE id=?", (item_id,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def set_description(self, item_id: int, text: str) -> None:
        self.db.execute("UPDATE tracking SET description=? WHERE id=?", (text, item_id))
        self.db.commit()

    def category_demand(self, brand: str, category: str, days: int = 14,
                        min_minutes: float = 10) -> tuple[float | None, int]:
        """Median likes/hour of comparable items we watched from their start."""
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT fav_last, (last_seen - first_seen) / 60.0 FROM tracking"
            " WHERE brand=? AND category=? AND first_seen>=? AND from_startup=0"
            " AND (last_seen - first_seen) >= ?",
            (brand, category, since, min_minutes * 60)).fetchall()
        rates = [fav / (mins / 60) for fav, mins in rows if mins > 0]
        if not rates:
            return None, 0
        return statistics.median(rates), len(rates)

    def record_alert(self, item, resale: float, profit: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO alerts (id, sent_at, total_price, resale, profit, title, url)"
            " VALUES (?,?,?,?,?,?,?)",
            (item.id, time.time(), item.total_price, resale, profit, item.title, item.url),
        )
        self.db.commit()

    def was_alerted(self, item_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE id=?", (item_id,)).fetchone() is not None
