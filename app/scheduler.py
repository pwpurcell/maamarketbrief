"""APScheduler-driven 15-minute refresh of all source fetchers into the SQLite cache.

The dashboard route reads from cache only; this module is what keeps the cache
fresh. On startup the first refresh runs immediately (in the background thread),
so the cache warms up while the server is already accepting requests.

Per-source failures are caught and logged — one broken source doesn't stop the
others from refreshing."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from app import cache
from app.config import REFRESH_INTERVAL_MINUTES
from app.sources import accc, asx_futures, demand_forecast, dwgm, equities, gbb, genmix, gsh, interconnectors, macro, nem, sttm, storage, wagbb, weather


log = logging.getLogger(__name__)


def _today_iso() -> str:
    return dt.date.today().isoformat()


def _yyyymmdd_to_iso(s: str | None) -> str:
    """genmix returns its gas_day as YYYYMMDD; coerce to ISO YYYY-MM-DD."""
    if not s:
        return _today_iso()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else s


# Fast 15-min jobs — AEMO files, RBA/FRED CSVs, ACCC monthly Excel, ASX equities.
FAST_JOBS: list[tuple[str, Callable[[], dict], Callable[[dict], str]]] = [
    ("gbb",      gbb.fetch,      lambda r: r["as_of_gas_day"]),
    ("sttm",     sttm.fetch,     lambda r: r["as_of_gas_day"]),
    ("dwgm",     dwgm.fetch,     lambda r: r["as_of_gas_day"]),
    ("gsh",      gsh.fetch,      lambda r: r["as_of_gas_day"]),
    # NEM dispatch is per-5-min; key by today's calendar date so each tick replaces today's row.
    ("nem",      nem.fetch,      lambda r: _today_iso()),
    ("interconnectors", interconnectors.fetch, lambda r: _today_iso()),
    ("genmix",   genmix.fetch,   lambda r: _yyyymmdd_to_iso(r.get("gas_day"))),
    ("wagbb",    wagbb.fetch,    lambda r: r["as_of_gas_day"]),
    ("macro",    macro.fetch,    lambda r: r["as_of_gas_day"]),
    ("accc",     accc.fetch,     lambda r: r["as_of_gas_day"]),
    ("equities", equities.fetch, lambda r: r["as_of_gas_day"]),
    ("weather",  weather.fetch,  lambda r: _today_iso()),
    ("storage",  storage.fetch,  lambda r: r["as_of_gas_day"]),
    ("demand",   demand_forecast.fetch, lambda r: r["as_of_gas_day"]),
    ("asx_futures", asx_futures.fetch, lambda r: _today_iso()),
]

# Slow jobs — currently empty. Reserved for future sources with strict daily rate limits.
SLOW_JOBS: list[tuple[str, Callable[[], dict], Callable[[dict], str]]] = []


def _run_jobs(jobs: list, label: str) -> dict[str, str]:
    cache.init()
    results: dict[str, str] = {}
    for source_name, fetcher, key_fn in jobs:
        try:
            log.info("[%s] refresh: %s", label, source_name)
            payload = fetcher()
            key = key_fn(payload)
            cache.put(source_name, key, payload)
            results[source_name] = f"ok ({key})"
        except Exception as exc:
            log.warning("[%s] refresh failed for %s: %s", label, source_name, exc, exc_info=True)
            results[source_name] = f"error: {type(exc).__name__}: {exc}"
    log.info("[%s] complete: %s", label, results)
    return results


def refresh_all() -> dict[str, str]:
    return _run_jobs(FAST_JOBS, "fast")


def refresh_slow() -> dict[str, str]:
    return _run_jobs(SLOW_JOBS, "slow")


def start() -> BackgroundScheduler:
    """Start a background scheduler.

    - Fast jobs (gbb, sttm, dwgm, gsh, nem, genmix, wagbb, macro, accc): every 15 min
    - Slow jobs (equities, rate-limited Alpha Vantage): once per day

    Both run immediately on startup so the cache is warm."""
    scheduler = BackgroundScheduler()
    now = dt.datetime.now()
    scheduler.add_job(
        refresh_all,
        "interval",
        minutes=REFRESH_INTERVAL_MINUTES,
        id="refresh_fast",
        next_run_time=now,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        refresh_slow,
        "interval",
        hours=24,
        id="refresh_slow",
        next_run_time=now,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    log.info("scheduler started; fast=%dmin, slow=24h", REFRESH_INTERVAL_MINUTES)
    return scheduler
