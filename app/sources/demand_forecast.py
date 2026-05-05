"""Demand forecasts — DWGM (int153) and STTM (int652).

Sources (AEMO public CSVs, no auth):
    DWGM forecast → int153_v4_demand_forecast_rpt_1.csv
        Columns: forecast_date, version_id, ti, forecast_demand_gj, ...
        Multiple `ti` (schedule intervals 1-5) per (forecast_date, version_id).
        Multiple version_ids as the day's forecast is revised.
        Daily forecast = sum(forecast_demand_gj) for the latest version_id
        across all `ti` values, per gas date.

    STTM scheduled quantity (≈ forecast demand) → int652_v1_ex_ante_schedule_quantity_rpt_1.csv
        Columns: gas_date, hub_identifier, hub_name, scheduled_qty, ...
        Multiple rows per (gas_date, hub) — one per supplying facility.
        Hub demand = sum(scheduled_qty) per (gas_date, hub_identifier).

Actuals comparison (forecast vs actual delta) deferred — DWGM actuals come
from int310 / int287 and STTM actuals are inferred from int657 imbalance
data. For Phase 5 we surface the forecast number on its own and show
actual as em-dash; comparison can come in a follow-up."""

from __future__ import annotations

import io
import logging

import httpx
import pandas as pd

from app.config import AEMO_HEADERS


DWGM_URL = "https://nemweb.com.au/Reports/Current/VicGas/int153_v4_demand_forecast_rpt_1.csv"
STTM_URL = "https://nemweb.com.au/Reports/Current/STTM/int652_v1_ex_ante_schedule_quantity_rpt_1.csv"

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=AEMO_HEADERS, timeout=30.0, follow_redirects=True) as client:
        dwgm = client.get(DWGM_URL); dwgm.raise_for_status()
        sttm = client.get(STTM_URL); sttm.raise_for_status()
    dwgm_data = _parse_dwgm(dwgm.text)
    sttm_data = _parse_sttm(sttm.text)
    return {
        "as_of_gas_day": max(dwgm_data["gas_day"], sttm_data["gas_day"]),
        "cards": [
            {
                "market": "DWGM (Victoria)",
                "forecast": dwgm_data["forecast_tj"],
                "actual": None,
                "unit": "TJ",
                "diff": None,
                "pct": None,
                "tone_class": "flat",
                "context": f"Forecast for gas day {dwgm_data['gas_day']} (AEMO int153)",
                "stale": False,
            },
            {
                "market": "STTM (SYD+BNE+ADL)",
                "forecast": sttm_data["forecast_tj"],
                "actual": None,
                "unit": "TJ",
                "diff": None,
                "pct": None,
                "tone_class": "flat",
                "context": f"Scheduled quantity for gas day {sttm_data['gas_day']} (AEMO int652)",
                "stale": False,
            },
        ],
    }


def _parse_dwgm(text: str) -> dict:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    df["forecast_date"] = pd.to_datetime(df["forecast_date"], format="%d %b %Y", errors="coerce")
    df = df.dropna(subset=["forecast_date"])
    df["forecast_demand_gj"] = pd.to_numeric(df["forecast_demand_gj"], errors="coerce").fillna(0)
    df["version_id"] = pd.to_numeric(df["version_id"], errors="coerce")
    df["ti"] = pd.to_numeric(df["ti"], errors="coerce")

    latest_date = df["forecast_date"].max()
    day_df = df[df["forecast_date"] == latest_date]
    latest_version = day_df["version_id"].max()
    latest_df = day_df[day_df["version_id"] == latest_version]
    # Sum across all schedule intervals (ti) for the latest version of the latest gas day.
    forecast_gj = float(latest_df.groupby("ti")["forecast_demand_gj"].max().sum())
    return {
        "gas_day": latest_date.date().isoformat(),
        "forecast_tj": round(forecast_gj / 1000.0, 1),
    }


def _parse_sttm(text: str) -> dict:
    df = pd.read_csv(io.StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    df["gas_date"] = pd.to_datetime(df["gas_date"], format="%d %b %Y", errors="coerce")
    df = df.dropna(subset=["gas_date"])
    df["scheduled_qty"] = pd.to_numeric(df["scheduled_qty"], errors="coerce").fillna(0)
    latest_date = df["gas_date"].max()
    day_df = df[df["gas_date"] == latest_date]
    # Total scheduled quantity at the hub level = sum across all supplying facilities, all hubs.
    forecast_gj = float(day_df["scheduled_qty"].sum())
    return {
        "gas_day": latest_date.date().isoformat(),
        "forecast_tj": round(forecast_gj / 1000.0, 1),
    }
