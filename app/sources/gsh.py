"""AEMO Wallumbilla Gas Supply Hub — daily benchmark price.

Source: https://nemweb.com.au/Reports/Current/GSH/Benchmark_Price/PUBLIC_WALLUMBILLABENCHMARKPRICE_<YYYYMMDD>.zip

Each file is a NEMWeb I/D/C zip-of-CSV containing a BENCHMARK_PRICE table.
PRODUCT_LOCATION values include WAL (Wallumbilla), MOOMBA, SEQ, CUL, WIL.
We surface the WAL benchmark price for the most recent gas day for which
IS_FIRM = 1 (i.e. the locked-in trade-weighted price)."""

from __future__ import annotations

import io
import logging
import re
import zipfile

import httpx
import pandas as pd

from app.config import AEMO_HEADERS
from app.sources._nemweb import parse_aemo_idr_csv


BASE_URL = "https://nemweb.com.au/Reports/Current/GSH/Benchmark_Price/"
FILENAME_RE = re.compile(r"PUBLIC_WALLUMBILLABENCHMARKPRICE_(\d{8})\.zip")

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=AEMO_HEADERS, timeout=60.0, follow_redirects=True) as client:
        listing = client.get(BASE_URL)
        listing.raise_for_status()
        dates = FILENAME_RE.findall(listing.text)
        if not dates:
            raise ValueError("No PUBLIC_WALLUMBILLABENCHMARKPRICE_<date>.zip found in GSH listing")
        latest_date = max(dates)
        zip_resp = client.get(f"{BASE_URL}PUBLIC_WALLUMBILLABENCHMARKPRICE_{latest_date}.zip")
        zip_resp.raise_for_status()
    df = _parse_zip(zip_resp.content)
    return _aggregate(df)


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"GSH zip contains no CSV (members: {zf.namelist()})")
        text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
    return parse_aemo_idr_csv(text, "BENCHMARK_PRICE")


def _aggregate(df: pd.DataFrame) -> dict:
    df = df.copy()
    df["GAS_DATE"] = pd.to_datetime(df["GAS_DATE"], errors="coerce")
    df["BENCHMARK_PRICE"] = pd.to_numeric(df["BENCHMARK_PRICE"], errors="coerce")
    df["IS_FIRM"] = pd.to_numeric(df["IS_FIRM"], errors="coerce").fillna(0).astype(int)
    df = df.dropna(subset=["GAS_DATE", "BENCHMARK_PRICE"])

    wal = df[df["PRODUCT_LOCATION"] == "WAL"]
    if wal.empty:
        raise ValueError("No WAL rows in GSH benchmark price file")

    firm = wal[wal["IS_FIRM"] == 1]
    chosen = firm if not firm.empty else wal
    latest = chosen.sort_values("GAS_DATE", ascending=False).iloc[0]
    return {
        "prices": [{
            "hub": "Wallumbilla GSH",
            "today": round(float(latest["BENCHMARK_PRICE"]), 2),
            "avg7": None,
            "stale": False,
        }],
        "as_of_gas_day": latest["GAS_DATE"].date().isoformat(),
    }
