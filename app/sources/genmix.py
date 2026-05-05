"""Generation mix — capacity factor per fuel category, gas day to date.

Combines:
    - DUID registry (app.sources.duid_registry) for DUID → fuel + Reg Cap
    - Next_Day_Dispatch (app.sources.dispatch_unit) for per-DUID MWh
    - Rooftop PV (app.sources.rooftop_pv) for behind-the-meter solar MWh

Capacity factor per category:
    CF = sum(MWh dispatched in category) / (sum(Reg Cap) × 24)

Special handling:
    - Battery: discharge CF computed on positive (gen) MWh. Round-trip
      throughput = gen MWh + load MWh.
    - Rooftop solar: no DUIDs, no Reg Cap from registry. Use the installed
      capacity constant in app.config.ROOFTOP_PV_INSTALLED_MW.

Caching: a 10-minute module-level TTL avoids hammering AEMO on every page
load (Next_Day_Dispatch is 8 MB, rooftop is 48 small files). Phase 5 will
replace this with proper SQLite caching."""

from __future__ import annotations

import datetime as dt
import logging
import time

import pandas as pd

from app.config import FUEL_CATEGORIES, ROOFTOP_PV_INSTALLED_MW
from app.sources import dispatch_unit, duid_registry, rooftop_pv


_CACHE: dict = {"data": None, "expires_at": 0.0}
_TTL_SECONDS = 600  # 10 minutes

log = logging.getLogger(__name__)


def fetch() -> dict:
    """Cached fetch of the latest gen-mix snapshot (capacity factors per fuel)."""
    now = time.time()
    if _CACHE["data"] is not None and now < _CACHE["expires_at"]:
        return _CACHE["data"]
    data = _fetch_fresh()
    _CACHE["data"] = data
    _CACHE["expires_at"] = now + _TTL_SECONDS
    return data


def _fetch_fresh() -> dict:
    registry = duid_registry.load_registry()
    dispatch = dispatch_unit.fetch_latest()
    gas_day_str = dispatch["gas_day"]
    gas_day = dt.date(int(gas_day_str[0:4]), int(gas_day_str[4:6]), int(gas_day_str[6:8])) if gas_day_str else None
    rooftop = rooftop_pv.fetch_for_gas_day(gas_day) if gas_day else None
    return aggregate(registry, dispatch["per_duid"], rooftop, gas_day_str)


def aggregate(
    registry: pd.DataFrame,
    per_duid: pd.DataFrame,
    rooftop: dict | None,
    gas_day_str: str | None,
) -> dict:
    """Combine inputs into one row per FUEL_CATEGORIES entry."""
    joined = per_duid.merge(registry, on="DUID", how="left")
    unmatched = joined["fuel_category"].isna().sum()
    if unmatched:
        log.warning("%d dispatch DUIDs not found in registry, skipped", unmatched)
    joined = joined.dropna(subset=["fuel_category"])

    by_cat = joined.groupby("fuel_category").agg(
        gen_mwh=("gen_mwh", "sum"),
        load_mwh=("load_mwh", "sum"),
    ).reset_index()

    cap_by_cat = registry.groupby("fuel_category")["reg_cap_mw"].sum().reset_index().rename(columns={"reg_cap_mw": "reg_cap_mw_sum"})
    summary = by_cat.merge(cap_by_cat, on="fuel_category", how="left")

    rows: list[dict] = []
    total_mwh = summary["gen_mwh"].sum()
    if rooftop:
        total_mwh += rooftop["total_mwh"]

    for category in FUEL_CATEGORIES:
        if category == "Rooftop solar (estimated)":
            if rooftop:
                rooftop_mwh = rooftop["total_mwh"]
                rows.append({
                    "fuel": category,
                    "cf": round(100 * rooftop_mwh / (ROOFTOP_PV_INSTALLED_MW * 24), 1),
                    "mwh": int(round(rooftop_mwh)),
                    "installed_mw": ROOFTOP_PV_INSTALLED_MW,
                    "avg7_cf": None,
                    "delta": None,
                    "share_pct": round(100 * rooftop_mwh / total_mwh, 1) if total_mwh else 0.0,
                    "stale": False,
                })
            else:
                rows.append(_empty_row(category, ROOFTOP_PV_INSTALLED_MW))
            continue

        cat_row = summary[summary["fuel_category"] == category]
        if cat_row.empty:
            rows.append(_empty_row(category, 0))
            continue

        gen_mwh = float(cat_row["gen_mwh"].iloc[0])
        load_mwh = float(cat_row["load_mwh"].iloc[0])
        cap_mw = float(cat_row["reg_cap_mw_sum"].iloc[0]) if pd.notna(cat_row["reg_cap_mw_sum"].iloc[0]) else 0.0
        cf = round(100 * gen_mwh / (cap_mw * 24), 1) if cap_mw > 0 else None
        share = round(100 * gen_mwh / total_mwh, 1) if total_mwh else 0.0

        row = {
            "fuel": category,
            "cf": cf,
            "mwh": int(round(gen_mwh)),
            "installed_mw": int(round(cap_mw)),
            "avg7_cf": None,
            "delta": None,
            "share_pct": share,
            "stale": False,
        }
        if category == "Battery":
            row["throughput_mwh"] = int(round(gen_mwh + load_mwh))
        rows.append(row)

    return {
        "gas_day": gas_day_str,
        "rows": rows,
    }


def _empty_row(category: str, installed_mw: int) -> dict:
    return {
        "fuel": category,
        "cf": None,
        "mwh": 0,
        "installed_mw": installed_mw,
        "avg7_cf": None,
        "delta": None,
        "share_pct": 0.0,
        "stale": True,
    }
