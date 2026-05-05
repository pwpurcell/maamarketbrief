"""SQLite cache for source fetch results.

Schema: a single `cache_blob` table keyed by (source, gas_day). The scheduler
upserts the latest fetch payload on each refresh tick; the dashboard route
reads from cache only — never hits AEMO directly. History (last N rows per
source, ordered by gas_day descending) feeds the 7-day sparklines.

For sub-daily sources like NEM dispatch the scheduler keys by today's
calendar date so each tick replaces the day's row with the latest 5-min
snapshot. Sparkline history therefore samples one snapshot per day."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any


DB_PATH = Path(__file__).resolve().parent / "data" / "cache.db"

log = logging.getLogger(__name__)


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH))
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init() -> None:
    """Create the cache table if it doesn't exist. Idempotent."""
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS cache_blob (
                source TEXT NOT NULL,
                gas_day TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                PRIMARY KEY (source, gas_day)
            )
        """)


def put(source: str, gas_day: str, payload: Any) -> None:
    """Upsert (source, gas_day) → payload. Payload is JSON-serialised."""
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO cache_blob (source, gas_day, fetched_at, payload) VALUES (?, ?, ?, ?)",
            (source, gas_day, dt.datetime.now(dt.timezone.utc).isoformat(), json.dumps(payload, default=str)),
        )


def get_latest(source: str) -> dict | None:
    """Most recent (source, gas_day) row by gas_day desc. Returns dict or None."""
    with _conn() as con:
        row = con.execute(
            "SELECT payload, fetched_at, gas_day FROM cache_blob WHERE source = ? ORDER BY gas_day DESC LIMIT 1",
            (source,),
        ).fetchone()
    if not row:
        return None
    return {"payload": json.loads(row[0]), "fetched_at": row[1], "gas_day": row[2]}


def get_history(source: str, n: int = 7) -> list[dict]:
    """Last N rows for `source` ordered chronologically (oldest first) for sparklines."""
    with _conn() as con:
        rows = con.execute(
            "SELECT payload, fetched_at, gas_day FROM cache_blob WHERE source = ? ORDER BY gas_day DESC LIMIT ?",
            (source, n),
        ).fetchall()
    return [
        {"payload": json.loads(r[0]), "fetched_at": r[1], "gas_day": r[2]}
        for r in reversed(rows)
    ]


def status() -> dict[str, dict | None]:
    """Diagnostic: return latest fetched_at + gas_day for every source in the cache."""
    with _conn() as con:
        rows = con.execute(
            "SELECT source, MAX(gas_day) AS gd, MAX(fetched_at) AS fa FROM cache_blob GROUP BY source"
        ).fetchall()
    return {r[0]: {"gas_day": r[1], "fetched_at": r[2]} for r in rows}
