"""AEMO STTM ex-ante market prices for Sydney, Adelaide, Brisbane hubs.

Source: https://nemweb.com.au/Reports/Current/STTM/int651_v1_ex_ante_market_price_rpt_1.csv

Plain header+data CSV (no I/D markers). Hub identifiers are the strings
SYD / ADL / BRI (the original BRIEF expected numeric IDs 1/2/3, but the
live file uses three-letter codes). One row per (gas_date, hub).
The latest gas_date in the file is the next gas day for which the ex-ante
price has been published."""

from __future__ import annotations

import io
import logging

import httpx
import pandas as pd

from app.config import AEMO_HEADERS


URL = "https://nemweb.com.au/Reports/Current/STTM/int651_v1_ex_ante_market_price_rpt_1.csv"

HUB_DISPLAY = {
    "SYD": "STTM Sydney",
    "ADL": "STTM Adelaide",
    "BRI": "STTM Brisbane",
}

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=AEMO_HEADERS, timeout=30.0, follow_redirects=True) as client:
        resp = client.get(URL)
        resp.raise_for_status()
    df = _parse_csv(resp.text)
    return _aggregate(df)


def _parse_csv(text: str) -> pd.DataFrame:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    required = {"gas_date", "hub_identifier", "ex_ante_market_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"STTM CSV missing columns: {sorted(missing)}")
    df["gas_date"] = pd.to_datetime(df["gas_date"], format="%d %b %Y", errors="coerce")
    df = df.dropna(subset=["gas_date"])
    return df


def _aggregate(df: pd.DataFrame) -> dict:
    latest_date = df["gas_date"].max()
    latest = df[df["gas_date"] == latest_date]
    rows = []
    for hub_id, display in HUB_DISPLAY.items():
        hub_rows = latest[latest["hub_identifier"] == hub_id]
        if hub_rows.empty:
            continue
        price = float(hub_rows["ex_ante_market_price"].iloc[0])
        rows.append({
            "hub": display,
            "today": round(price, 2),
            "avg7": None,
            "stale": False,
        })
    return {"prices": rows, "as_of_gas_day": latest_date.date().isoformat()}
