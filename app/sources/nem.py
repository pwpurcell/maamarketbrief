"""AEMO NEM 5-minute dispatch RRP per region.

Source: https://nemweb.com.au/Reports/Current/DispatchIS_Reports/PUBLIC_DISPATCHIS_<timestamp>_<seq>.zip

Files are timestamped every 5 minutes. Each zip contains a single CSV in
NEMWeb I/D/C format with multiple tables; we want the PRICE table, columns
SETTLEMENTDATE, REGIONID, RRP, INTERVENTION. Filter to INTERVENTION = "0"
to skip intervention re-runs and surface the most recent dispatch RRP per
region."""

from __future__ import annotations

import io
import logging
import re
import zipfile

import httpx
import pandas as pd

from app.config import AEMO_HEADERS, NEM_REGIONS
from app.sources._nemweb import parse_aemo_idr_csv


BASE_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
FILENAME_RE = re.compile(r"PUBLIC_DISPATCHIS_\d+_\d+\.zip")

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=AEMO_HEADERS, timeout=60.0, follow_redirects=True) as client:
        listing = client.get(BASE_URL)
        listing.raise_for_status()
        files = FILENAME_RE.findall(listing.text)
        if not files:
            raise ValueError("No PUBLIC_DISPATCHIS_*.zip files in DispatchIS listing")
        latest = sorted(files)[-1]
        zip_resp = client.get(BASE_URL + latest)
        zip_resp.raise_for_status()
    df = _parse_zip(zip_resp.content)
    return _aggregate(df)


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"DispatchIS zip contains no CSV (members: {zf.namelist()})")
        text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
    return parse_aemo_idr_csv(text, "PRICE")


def _aggregate(df: pd.DataFrame) -> dict:
    df = df.copy()
    df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"], errors="coerce").fillna(0).astype(int)
    df = df[df["INTERVENTION"] == 0]
    df["SETTLEMENTDATE"] = pd.to_datetime(df["SETTLEMENTDATE"], errors="coerce")
    df["RRP"] = pd.to_numeric(df["RRP"], errors="coerce")
    df = df.dropna(subset=["SETTLEMENTDATE", "RRP", "REGIONID"])

    rows = []
    for region in NEM_REGIONS:
        region_df = df[df["REGIONID"] == region]
        if region_df.empty:
            continue
        latest = region_df.sort_values("SETTLEMENTDATE", ascending=False).iloc[0]
        rows.append({
            "region": region,
            "rrp": round(float(latest["RRP"]), 2),
            "avg7": None,
            "stale": False,
        })
    return {
        "prices": rows,
        "as_of": df["SETTLEMENTDATE"].max().isoformat(),
    }
