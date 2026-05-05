"""AEMO ROOFTOP_PV/ACTUAL — half-hourly satellite-derived rooftop solar MW per region.

Source: https://nemweb.com.au/Reports/Current/ROOFTOP_PV/ACTUAL/PUBLIC_ROOFTOP_PV_ACTUAL_SATELLITE_<YYYYMMDDHHMM00>_<seq>.zip

Each file is a single half-hourly snapshot (one ACTUAL row per region:
NSW1, QLD1, SA1, TAS1, VIC1). To get a daily MWh number we fetch all 48
half-hourly files for the target gas day and sum, then divide by 2 (MW
half-hours → MWh).

Trade-off: 48 small HTTP requests per refresh. Phase 5 caching will make
this cheap; for now the genmix module wraps fetch() in a TTL cache to
avoid hammering AEMO."""

from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import re
import zipfile

import httpx
import pandas as pd

from app.config import AEMO_HEADERS
from app.sources._nemweb import parse_aemo_idr_csv


BASE_URL = "https://nemweb.com.au/Reports/Current/ROOFTOP_PV/ACTUAL/"
FILENAME_RE = re.compile(r"PUBLIC_ROOFTOP_PV_ACTUAL_SATELLITE_(\d{14})_\d+\.zip")

log = logging.getLogger(__name__)


def fetch_for_gas_day(gas_day: dt.date) -> dict:
    """Fetch all half-hourly rooftop PV files for the gas day starting `gas_day` 06:00.

    Returns total MWh across all NEM regions for that gas day (06:00 → 06:00 next day)."""
    start = dt.datetime.combine(gas_day, dt.time(6, 0))
    end = start + dt.timedelta(days=1)

    with httpx.Client(headers=AEMO_HEADERS, timeout=30.0, follow_redirects=True) as client:
        listing = client.get(BASE_URL)
        listing.raise_for_status()
        all_files = FILENAME_RE.findall(listing.text)
        in_window = [
            ts for ts in all_files
            if start <= dt.datetime.strptime(ts, "%Y%m%d%H%M%S") < end
        ]
    if not in_window:
        raise ValueError(f"No ROOFTOP_PV files found for gas day starting {gas_day}")

    # Re-resolve each timestamp's full filename (with seq suffix) from the listing.
    full_names = re.findall(r"PUBLIC_ROOFTOP_PV_ACTUAL_SATELLITE_\d{14}_\d+\.zip", listing.text)
    targets = [n for n in full_names if FILENAME_RE.match(n).group(1) in in_window]

    rows = asyncio.run(_fetch_all(targets))
    return _aggregate(rows, gas_day)


async def _fetch_all(filenames: list[str]) -> list[pd.DataFrame]:
    """Fetch all files with limited concurrency. Skip individual file failures
    rather than aborting — missing 1-2 of 48 half-hours doesn't materially shift
    the daily MWh number, and AEMO occasionally 403s under parallel load."""
    async with httpx.AsyncClient(headers=AEMO_HEADERS, timeout=30.0, follow_redirects=True) as client:
        sem = asyncio.Semaphore(3)

        async def one(name: str):
            async with sem:
                try:
                    r = await client.get(BASE_URL + name)
                    r.raise_for_status()
                    return _parse_zip(r.content)
                except Exception as exc:
                    log.warning("rooftop_pv: skipping %s (%s)", name, exc)
                    return None

        results = await asyncio.gather(*(one(n) for n in filenames))
        frames = [r for r in results if r is not None]
        if not frames:
            raise RuntimeError("All ROOFTOP_PV file fetches failed")
        log.info("rooftop_pv: fetched %d/%d intervals", len(frames), len(filenames))
        return frames


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"ROOFTOP_PV zip contains no CSV (members: {zf.namelist()})")
        text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
    return parse_aemo_idr_csv(text, "ACTUAL")


def _aggregate(frames: list[pd.DataFrame], gas_day: dt.date) -> dict:
    df = pd.concat(frames, ignore_index=True)
    df["POWER"] = pd.to_numeric(df["POWER"], errors="coerce").fillna(0.0)
    # Each row is MW for a half-hour interval. Sum MW × 0.5 hours → MWh.
    total_mwh = float(df["POWER"].sum() / 2.0)
    intervals = df["INTERVAL_DATETIME"].nunique()
    return {
        "gas_day": gas_day.isoformat(),
        "total_mwh": total_mwh,
        "intervals_used": intervals,
    }
