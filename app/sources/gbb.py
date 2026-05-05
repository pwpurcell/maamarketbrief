"""AEMO Gas Bulletin Board — east coast pipeline nominations + forecast.

Source: https://nemweb.com.au/Reports/Current/GBB/GasBBNominationAndForecast.zip

Why nominations and not actuals: AEMO's `GasBBActualFlowStorage` report is
metered, settled flow data and lags by 1–2 gas days. The Nomination + Forecast
report is what shippers have booked to flow today and over the forward 7-day
horizon, updated multiple times daily. It's also what gbb.aemo.com.au shows
in its "today" view, so what we display here matches that.

Schema is almost identical to ActualFlowStorage with two differences:
    - date column is `Gasdate` (lowercase 'd'), not `GasDate`
    - no HeldInStorage / CushionGasStorage columns

The Demand/Supply/TransferIn/TransferOut semantics are the same so per-pipeline
flow rules in app.config.PIPELINES carry over unchanged."""

from __future__ import annotations

import datetime as dt
import io
import logging
import zipfile
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

from app.config import AEMO_HEADERS, PIPELINES, PipelineSpec


BASE_URL = "https://nemweb.com.au/Reports/Current/GBB/"
ZIP_NAME = "GasBBNominationAndForecast.zip"
DELTA_FLAT_THRESHOLD_TJD = 2.0
MELB_TZ = ZoneInfo("Australia/Melbourne")

log = logging.getLogger(__name__)


def fetch() -> dict:
    """Download the latest GBB nominations file and aggregate per pipeline for today's gas day."""
    url = BASE_URL + ZIP_NAME
    with httpx.Client(headers=AEMO_HEADERS, timeout=60.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
    df = _parse_zip(resp.content)
    return _aggregate(df)


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"GBB zip contains no CSV (members: {zf.namelist()})")
        with zf.open(csv_names[0]) as f:
            df = pd.read_csv(f)
    df.columns = [c.strip() for c in df.columns]
    required = {"Gasdate", "FacilityName", "LocationName", "Demand", "Supply", "TransferIn", "TransferOut"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"GBB nominations CSV missing columns: {sorted(missing)}")
    df["Gasdate"] = pd.to_datetime(df["Gasdate"], format="%Y/%m/%d", errors="coerce")
    df = df.dropna(subset=["Gasdate", "FacilityName"])
    return df


def _today_gas_day() -> pd.Timestamp:
    """Gas day starts at 06:00 Australia/Melbourne time. Returns today's gas day as a Timestamp."""
    now = dt.datetime.now(MELB_TZ)
    gas_day = now.date() if now.hour >= 6 else now.date() - dt.timedelta(days=1)
    return pd.Timestamp(gas_day)


def _resolve_gas_day(df: pd.DataFrame, target: pd.Timestamp) -> pd.Timestamp:
    """Use target if present, else fall back to the most recent published gas day at or before target."""
    available = df["Gasdate"].unique()
    if target in available:
        return target
    earlier = [d for d in available if d <= target]
    if not earlier:
        raise ValueError(f"No GBB nominations on or before {target.date()}")
    return max(earlier)


def _aggregate(df: pd.DataFrame, today: pd.Timestamp | None = None) -> dict:
    today_target = today if today is not None else _today_gas_day()
    today_day = _resolve_gas_day(df, today_target)
    yest_day = today_day - pd.Timedelta(days=1)
    today_df = df[df["Gasdate"] == today_day]
    yest_df = df[df["Gasdate"] == yest_day]

    rows = []
    for spec in PIPELINES:
        if spec.facility is None:
            continue
        flow_today = _flow_for(today_df, spec)
        flow_yest = _flow_for(yest_df, spec)
        rows.append(_pipeline_row(spec, flow_today, flow_yest))

    return {
        "as_of_gas_day": today_day.date().isoformat(),
        "pipelines": rows,
        "stale": False,
    }


def _flow_for(day_df: pd.DataFrame, spec: PipelineSpec) -> float:
    rows = day_df[day_df["FacilityName"].str.lower() == spec.facility.lower()]
    if spec.location:
        rows = rows[rows["LocationName"].str.contains(spec.location, case=False, na=False)]
    if not spec.flow_columns:
        return 0.0
    return float(rows[list(spec.flow_columns)].sum().sum())


def _pipeline_row(spec: PipelineSpec, flow_today: float, flow_yest: float) -> dict:
    delta = flow_today - flow_yest
    if abs(delta) <= DELTA_FLAT_THRESHOLD_TJD:
        arrow, cls = "→", "flat"
    elif delta > 0:
        arrow, cls = "↑", "up"
    else:
        arrow, cls = "↓", "down"
    util = round(100 * flow_today / spec.nameplate_tjd, 1) if spec.nameplate_tjd else 0.0
    return {
        "id": spec.id,
        "display": spec.display,
        "direction": spec.direction,
        "flow": round(flow_today, 1),
        "nameplate": spec.nameplate_tjd,
        "utilisation": util,
        "delta_arrow": arrow,
        "delta_class": cls,
        "delta_value": round(delta, 1),
        "stale": False,
    }
