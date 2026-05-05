"""AEMO Next_Day_Dispatch — per-DUID 5-minute dispatch for the prior gas day.

Source: https://nemweb.com.au/Reports/Current/Next_Day_Dispatch/PUBLIC_NEXT_DAY_DISPATCH_<YYYYMMDD>_<seq>.zip

Each file is published once per gas day (~12:30 the next day) and contains
the full UNIT_SOLUTION table — 288 five-minute intervals × ~560 DUIDs.
We extract TOTALCLEARED (the actual dispatched MW for that interval) and
sum per DUID, then divide by 12 to convert MW-intervals to MWh.

Filter:
    - INTERVENTION = 0 (skip intervention re-runs)
    - TRADETYPE = 0 (energy only, not FCAS)"""

from __future__ import annotations

import io
import logging
import re
import zipfile

import httpx
import pandas as pd

from app.config import AEMO_HEADERS
from app.sources._nemweb import parse_aemo_idr_csv


BASE_URL = "https://nemweb.com.au/Reports/Current/Next_Day_Dispatch/"
FILENAME_RE = re.compile(r"PUBLIC_NEXT_DAY_DISPATCH_(\d{8})_\d+\.zip")

log = logging.getLogger(__name__)


def fetch_latest() -> dict:
    """Fetch the most recently published Next_Day_Dispatch file and return per-DUID MWh for that gas day."""
    with httpx.Client(headers=AEMO_HEADERS, timeout=120.0, follow_redirects=True) as client:
        listing = client.get(BASE_URL)
        listing.raise_for_status()
        files = sorted(re.findall(r'PUBLIC_NEXT_DAY_DISPATCH_\d{8}_\d+\.zip', listing.text))
        if not files:
            raise ValueError("No PUBLIC_NEXT_DAY_DISPATCH files found")
        latest = files[-1]
        m = FILENAME_RE.match(latest)
        gas_day = m.group(1) if m else None
        zip_resp = client.get(BASE_URL + latest)
        zip_resp.raise_for_status()
    df = _parse_zip(zip_resp.content)
    return _aggregate(df, gas_day_str=gas_day)


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"Next_Day_Dispatch zip contains no CSV (members: {zf.namelist()})")
        text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
    return parse_aemo_idr_csv(text, "UNIT_SOLUTION")


def _aggregate(df: pd.DataFrame, gas_day_str: str | None = None) -> dict:
    """Sum TOTALCLEARED per DUID, divide by 12 to convert MW-intervals → MWh."""
    df = df.copy()
    df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"], errors="coerce").fillna(-1).astype(int)
    df["TRADETYPE"] = pd.to_numeric(df["TRADETYPE"], errors="coerce").fillna(-1).astype(int)
    df = df[(df["INTERVENTION"] == 0) & (df["TRADETYPE"] == 0)]

    df["TOTALCLEARED"] = pd.to_numeric(df["TOTALCLEARED"], errors="coerce").fillna(0.0)

    # Battery generator DUIDs report negative TOTALCLEARED when charging.
    # For per-DUID generation MWh we want only the positive (discharging / generating) intervals.
    # The brief calls for tracking discharge CF + round-trip throughput separately for batteries,
    # so we emit both totals.
    df["positive_mw"] = df["TOTALCLEARED"].clip(lower=0)
    df["negative_mw"] = df["TOTALCLEARED"].clip(upper=0)

    grouped = df.groupby("DUID").agg(
        gen_mw_intervals_sum=("positive_mw", "sum"),
        load_mw_intervals_sum=("negative_mw", "sum"),  # negative
        intervals=("TOTALCLEARED", "size"),
    )
    grouped["gen_mwh"] = grouped["gen_mw_intervals_sum"] / 12.0
    grouped["load_mwh"] = (-grouped["load_mw_intervals_sum"]) / 12.0
    grouped = grouped.reset_index()

    return {
        "gas_day": gas_day_str,
        "per_duid": grouped[["DUID", "gen_mwh", "load_mwh"]],
    }
