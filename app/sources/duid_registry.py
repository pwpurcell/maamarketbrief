"""AEMO NEM Registration & Exemption List — DUID → fuel category lookup.

Source: https://www.aemo.com.au/-/media/Files/Electricity/NEM/Participant_Information/NEM-Registration-and-Exemption-List.xls

(Despite the .xls extension AEMO publishes this as XLSX, hence openpyxl.)

The "PU and Scheduled Loads" sheet has one row per DUID with `Fuel Source -
Descriptor` (e.g. "Black Coal", "Natural Gas", "Coal Seam Methane") and
`Reg Cap generation (MW)`. We map descriptors to display fuel categories,
identify batteries via `Fuel Source - Primary` = "Battery Storage", and
cache the normalised result locally as parquet so the dashboard doesn't
re-download a 770KB XLSX on every page load.

Cache refresh policy: parquet older than CACHE_TTL_DAYS triggers a re-fetch.
The job can also be triggered manually by calling load_registry(force_refresh=True)."""

from __future__ import annotations

import datetime as dt
import io
import logging
from pathlib import Path

import httpx
import pandas as pd

from app.config import AEMO_HEADERS


URL = "https://www.aemo.com.au/-/media/Files/Electricity/NEM/Participant_Information/NEM-Registration-and-Exemption-List.xls"
SHEET = "PU and Scheduled Loads"
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "duid_registry.parquet"
CACHE_TTL_DAYS = 7

# Map AEMO `Fuel Source - Descriptor` (lower-cased, whitespace-stripped)
# to our display fuel category. Categories must match app.config.FUEL_CATEGORIES.
DESCRIPTOR_TO_CATEGORY: dict[str, str] = {
    "black coal":                       "Black coal",
    "brown coal":                       "Brown coal",
    "natural gas":                      "Gas",
    "coal seam methane":                "Gas",
    "waste coal mine gas":              "Gas",
    "natural gas / fuel oil":           "Gas",
    "natural gas / diesel":             "Gas",
    "natrual gas/ diesel":              "Gas",   # AEMO typo (sic)
    "ethane":                           "Gas",
    "water":                            "Hydro",
    "wind":                             "Wind",
    "solar":                            "Utility solar",
    "diesel":                           "Diesel",
    "kerosene":                         "Diesel",
    "bagasse":                          "Biomass / other",
    "landfill methane / landfill gas":  "Biomass / other",
    "biogas - sludge":                  "Biomass / other",
    "sewerage / waste water":           "Biomass / other",
    "bagasse and diesel":               "Biomass / other",
}

VALID_CLASSIFICATIONS = {"scheduled", "semi-scheduled", "non-scheduled"}

log = logging.getLogger(__name__)


def load_registry(force_refresh: bool = False) -> pd.DataFrame:
    """Return the normalised DUID registry, refreshing from AEMO if cache is stale or missing."""
    if not force_refresh and _cache_is_fresh():
        log.debug("DUID registry cache hit at %s", CACHE_PATH)
        return pd.read_parquet(CACHE_PATH)
    log.info("Refreshing DUID registry from %s", URL)
    df = _fetch_and_normalise()
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(CACHE_PATH)
    return df


def _cache_is_fresh() -> bool:
    if not CACHE_PATH.exists():
        return False
    mtime = dt.datetime.fromtimestamp(CACHE_PATH.stat().st_mtime)
    age = dt.datetime.now() - mtime
    return age.days < CACHE_TTL_DAYS


def _fetch_and_normalise() -> pd.DataFrame:
    with httpx.Client(headers=AEMO_HEADERS, timeout=120.0, follow_redirects=True) as client:
        resp = client.get(URL)
        resp.raise_for_status()
    return normalise(pd.read_excel(io.BytesIO(resp.content), sheet_name=SHEET, engine="openpyxl"))


def normalise(df: pd.DataFrame) -> pd.DataFrame:
    """Trim, filter, and tag DUIDs with our display fuel category + Reg Cap."""
    df = df.copy()
    df["DUID"] = df["DUID"].astype(str).str.strip()
    df = df[df["DUID"].notna() & (df["DUID"] != "-") & (df["DUID"] != "nan")]

    descriptor_clean = df["Fuel Source - Descriptor"].astype(str).str.strip().str.lower()
    primary_clean = df["Fuel Source - Primary"].astype(str).str.strip().str.lower()

    df["fuel_category"] = descriptor_clean.map(DESCRIPTOR_TO_CATEGORY)
    df.loc[primary_clean.str.contains("battery", na=False), "fuel_category"] = "Battery"

    classification_clean = (
        df["Classification"].astype(str)
        .str.strip()
        .str.replace("*", "", regex=False)
        .str.lower()
    )
    df = df[classification_clean.isin(VALID_CLASSIFICATIONS)]

    df["reg_cap_mw"] = pd.to_numeric(df["Reg Cap generation (MW)"], errors="coerce")
    df = df[df["fuel_category"].notna() & (df["reg_cap_mw"].fillna(0) > 0)]

    return df[["DUID", "Station Name", "Region", "Classification", "fuel_category", "reg_cap_mw"]].reset_index(drop=True)
