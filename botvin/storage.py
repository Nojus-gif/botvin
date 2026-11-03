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
    description TEXT                          -- NULL = item page not fetched yet
);
CREATE INDEX IF NOT EXISTS tracking_bc ON tracking(brand, category, first_seen);
-- What happened to promising items ~2 h after listing (learning.py).
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    reviewed_at REAL NOT NULL,
    status TEXT NOT NULL,          -- sold | unsold
    sold_how TEXT,                 -- page | watch | not in search | page error + not in search
    sold_minutes REAL,             -- when known (caught by a watchlist check)
    alerted INTEGER,
    brand TEXT, category TEXT, gender TEXT, size_letter TEXT, colour TEXT,
    price REAL, resale REAL, profit REAL, margin REAL, margin_band TEXT,
    demand_level TEXT, likes INTEGER, title TEXT, url TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
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


EXTRA_TRACKING_COLUMNS = [
    ("uploaded_at", "REAL"),            # from the item page, if found
    ("next_check", "REAL"),             # watchlist: when to re-open the item page
    ("checks", "INTEGER DEFAULT 0"),    # how many times we re-checked it
    ("sold_at", "REAL"),                # when we noticed it sold
    ("item_json", "TEXT"),              # enough of the listing to rebuild it
    ("followup", "INTEGER DEFAULT 0"),  # 1 = "demand confirmed" already sent
    ("gender", "TEXT"),                 # from the item page
    ("colour", "TEXT"),
    ("reviewed", "INTEGER DEFAULT 0"),  # 1 = outcome recorded
]


class Store:
    def __init__(self, path: str | Path = "botvin.db"):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        # Columns added by later versions; add them to older databases.
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(tracking)")}
        for col, decl in EXTRA_TRACKING_COLUMNS:
            if col not in cols:
                self.db.execute(f"ALTER TABLE tracking ADD COLUMN {col} {decl}")
        self.db.commit()

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

    def set_page_info(self, item_id: int, description: str, uploaded_at: float | None,
                      gender: str | None = None, colour: str | None = None) -> None:
        self.db.execute("UPDATE tracking SET description=?, uploaded_at=?, gender=?, colour=? WHERE id=?",
                        (description, uploaded_at, gender, colour, item_id))
        self.db.commit()

    def category_demand(self, brand: str, category: str, days: int = 14,
                        min_minutes: float = 10) -> tuple[float | None, int]:
        """Median likes/hour of comparable items we watched from their start."""
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT fav_last, (last_seen - COALESCE(uploaded_at, first_seen)) / 60.0 FROM tracking"
            " WHERE brand=? AND category=? AND first_seen>=?"
            " AND (from_startup=0 OR uploaded_at IS NOT NULL)"
            " AND (last_seen - COALESCE(uploaded_at, first_seen)) >= ?",
            (brand, category, since, min_minutes * 60)).fetchall()
        rates = [fav / (mins / 60) for fav, mins in rows if mins > 0]
        if not rates:
            return None, 0
        return statistics.median(rates), len(rates)

    # ------------------------------------------------------------ watchlist
    def watch(self, item_id: int, next_check: float, item_json: str) -> None:
        self.db.execute("UPDATE tracking SET next_check=COALESCE(next_check, ?), item_json=? WHERE id=?",
                        (next_check, item_json, item_id))

    def due_checks(self, now: float, limit: int) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM tracking WHERE next_check IS NOT NULL AND next_check<=? AND sold_at IS NULL"
            " ORDER BY next_check LIMIT ?", (now, limit))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def update_check(self, item_id: int, *, likes: int | None, views: int | None,
                     sold: bool, next_check: float | None, now: float | None = None) -> None:
        now = now or time.time()
        self.db.execute(
            "UPDATE tracking SET checks=checks+1, last_seen=?, next_check=?,"
            " fav_last=MAX(fav_last, COALESCE(?, fav_last)), views_last=MAX(views_last, COALESCE(?, views_last)),"
            " sold_at=CASE WHEN ? THEN ? ELSE sold_at END WHERE id=?",
            (now, next_check, likes, views, int(sold), now, item_id))
        self.db.commit()

    def set_followup(self, item_id: int) -> None:
        self.db.execute("UPDATE tracking SET followup=1 WHERE id=?", (item_id,))
        self.db.commit()

    def sell_through(self, brand: str, category: str, within_minutes: float = 60,
                     days: int = 30) -> tuple[int, int]:
        """(sold within N minutes, watched long enough to tell) for brand+category."""
        since = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT COALESCE(uploaded_at, first_seen), sold_at, last_seen FROM tracking"
            " WHERE brand=? AND category=? AND first_seen>=? AND checks>0",
            (brand, category, since)).fetchall()
        sold = watched = 0
        for start, sold_at, last in rows:
            if sold_at and (sold_at - start) / 60 <= within_minutes:
                sold += 1; watched += 1
            elif (last - start) / 60 >= within_minutes:
                watched += 1
        return sold, watched

    # -------------------------------------------------------------- outcomes
    def due_reviews(self, now: float, after_seconds: float, limit: int) -> list[dict]:
        cur = self.db.execute(
            "SELECT * FROM tracking WHERE item_json IS NOT NULL AND COALESCE(reviewed,0)=0"
            " AND COALESCE(uploaded_at, first_seen) <= ? ORDER BY first_seen LIMIT ?",
            (now - after_seconds, limit))
        names = [c[0] for c in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]

    def record_outcome(self, item_id: int, **fields) -> None:
        cols = ["id", "reviewed_at"] + list(fields)
        self.db.execute(
            f"INSERT OR REPLACE INTO outcomes ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            [item_id, time.time(), *fields.values()])
        self.db.execute("UPDATE tracking SET reviewed=1 WHERE id=?", (item_id,))
        self.db.commit()

    def mark_reviewed(self, item_id: int) -> None:
        self.db.execute("UPDATE tracking SET reviewed=1 WHERE id=?", (item_id,))
        self.db.commit()

    def outcome_counts(self, where: dict, status: str | None = None) -> tuple[int, int]:
        """(sold, sold+unsold) matching `where`; with status=..., (n, n) of that status."""
        clauses = [f"{k}=?" for k in where]
        args = list(where.values())
        if status:
            clauses.append("status=?"); args.append(status)
            n = self.db.execute(f"SELECT COUNT(*) FROM outcomes WHERE {' AND '.join(clauses)}",
                                args).fetchone()[0]
            return n, n
        clauses.append("status IN ('sold','unsold')")
        row = self.db.execute(
            f"SELECT SUM(status='sold'), COUNT(*) FROM outcomes WHERE {' AND '.join(clauses)}",
            args).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def outcomes_since(self, since: float) -> list[tuple]:
        """[(status, title, price, brand, category, likes)] reviewed after `since`."""
        return self.db.execute(
            "SELECT status, title, price, brand, category, likes FROM outcomes"
            " WHERE reviewed_at > ? AND status IN ('sold','unsold') ORDER BY reviewed_at",
            (since,)).fetchall()

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))
        self.db.commit()

    def outcome_breakdown(self, column_expr: str) -> list[tuple]:
        """[(key, sold, total, avg sold_minutes)] grouped by a column expression
        (fixed strings from learning.report, never user input)."""
        return self.db.execute(
            f"SELECT {column_expr} AS k, SUM(status='sold'), COUNT(*),"
            " AVG(CASE WHEN status='sold' THEN sold_minutes END)"
            " FROM outcomes WHERE status IN ('sold','unsold') GROUP BY k").fetchall()

    def record_alert(self, item, resale: float, profit: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO alerts (id, sent_at, total_price, resale, profit, title, url)"
            " VALUES (?,?,?,?,?,?,?)",
            (item.id, time.time(), item.total_price, resale, profit, item.title, item.url),
        )
        self.db.commit()

    def was_alerted(self, item_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM alerts WHERE id=?", (item_id,)).fetchone() is not None
