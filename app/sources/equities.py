"""ASX-listed energy equities via Markit Digital's ASX backend.

Source: https://asx.api.markitdigital.com/asx-research/1.0/companies/<TICKER>/header

This is the JSON backend that powers asx.com.au's company pages — it's not a
formally documented public API, but it's been stable for years and serves
the same data the ASX displays on its own website. No API key required.
For each ticker we get priceLast, priceChange, priceChangePercent, marketCap,
volume, displayName, and sector in a single call.

Notes:
- priceChange/priceChangePercent are intraday change vs prior close. They
  may show 0 outside market hours (10:00-16:00 AEST/AEDT on trading days).
- marketCap is in dollars (we convert to billions for display).
- 30-day percent change isn't on this endpoint; left as None for now."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

import httpx


BASE_URL = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{ticker}/header"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (markets-brief; non-commercial)",
    "Accept": "application/json",
}

# (ticker, company name, segment)
TICKERS: list[tuple[str, str, str]] = [
    ("AEL", "Amplitude Energy",          "Upstream gas (formerly Cooper Energy)"),
    ("AGL", "AGL Energy",                "Electricity / gas retail + generation"),
    ("ALD", "Ampol",                     "Fuels (refining + retail)"),
    ("APA", "APA Group",                 "Gas pipelines + electricity transmission"),
    ("BPT", "Beach Energy",              "Upstream gas / oil"),
    ("KAR", "Karoon Energy",             "Upstream oil"),
    ("NHC", "New Hope Corporation",      "Coal"),
    ("ORG", "Origin Energy",             "Electricity / gas retail + APLNG stake"),
    ("SMR", "Stanmore Resources",        "Coal"),
    ("STO", "Santos",                    "Upstream gas / LNG"),
    ("STX", "Strike Energy",             "Upstream gas"),
    ("VEA", "Viva Energy",               "Fuels (refining + retail)"),
    ("WDS", "Woodside Energy",           "LNG / oil"),
    ("WHC", "Whitehaven Coal",           "Coal"),
    ("YAL", "Yancoal Australia",         "Coal"),
]

log = logging.getLogger(__name__)


def fetch() -> dict:
    rows = asyncio.run(_fetch_all())
    return {"as_of_gas_day": dt.date.today().isoformat(), "rows": rows}


async def _fetch_all() -> list[dict]:
    async with httpx.AsyncClient(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(4)

        async def one(ticker: str, name: str, segment: str) -> dict:
            async with sem:
                try:
                    resp = await client.get(BASE_URL.format(ticker=ticker))
                    if resp.status_code != 200:
                        log.warning("Markit Digital %d for %s: %s", resp.status_code, ticker, resp.text[:200])
                        return _empty(ticker, name, segment)
                    body = resp.json()
                    data = body.get("data") or {}
                    return _row(ticker, name, segment, data)
                except Exception as exc:
                    log.warning("equities: %s failed: %s", ticker, exc)
                    return _empty(ticker, name, segment)

        return await asyncio.gather(*(one(t, n, s) for t, n, s in TICKERS))


def _row(ticker: str, name: str, segment: str, data: dict) -> dict:
    last = data.get("priceLast")
    change_pct = data.get("priceChangePercent")
    mcap = data.get("marketCap")
    return {
        "ticker": ticker,
        "company": name,
        "segment": segment,
        "last": round(float(last), 2) if last is not None else None,
        "delta_today_pct": round(float(change_pct), 2) if change_pct is not None else None,
        "delta_30d_pct": None,  # not available on this endpoint
        "mcap_bn": round(float(mcap) / 1e9, 2) if mcap else None,
        "stale": last is None,
    }


def _empty(ticker: str, name: str, segment: str) -> dict:
    return {
        "ticker": ticker,
        "company": name,
        "segment": segment,
        "last": None,
        "delta_today_pct": None,
        "delta_30d_pct": None,
        "mcap_bn": None,
        "stale": True,
    }
