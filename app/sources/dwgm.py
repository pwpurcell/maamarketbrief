"""AEMO Declared Wholesale Gas Market (Victoria) — published schedule prices.

Source: https://nemweb.com.au/Reports/Current/VicGas/int041_v4_market_and_reference_prices_1.csv

Plain header+data CSV. One row per gas_date with five published prices for
the day's five schedules: BOD (6am), 10am, 2pm, 6pm, 10pm — each is the
clearing price set when DWGM re-runs the market schedule. We surface the
6am price (price_bod_gst_ex) since that's the canonical "DWGM 6am" number
the brief asks for."""

from __future__ import annotations

import io
import logging

import httpx
import pandas as pd

from app.config import AEMO_HEADERS


URL = "https://nemweb.com.au/Reports/Current/VicGas/int041_v4_market_and_reference_prices_1.csv"

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
    if "gas_date" not in df.columns or "price_bod_gst_ex" not in df.columns:
        raise ValueError(f"DWGM CSV missing required columns; got {list(df.columns)}")
    df["gas_date"] = pd.to_datetime(df["gas_date"], format="%d %b %Y", errors="coerce")
    df = df.dropna(subset=["gas_date"])
    return df


def _aggregate(df: pd.DataFrame) -> dict:
    latest_date = df["gas_date"].max()
    row = df[df["gas_date"] == latest_date].iloc[0]
    price = float(row["price_bod_gst_ex"])
    return {
        "prices": [{
            "hub": "DWGM (6am)",
            "today": round(price, 2),
            "avg7": None,
            "stale": False,
        }],
        "as_of_gas_day": latest_date.date().isoformat(),
    }
