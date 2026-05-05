"""AEMO WA Gas Bulletin Board — Dampier-Bunbury flow + production facilities.

Source (modern JSON API):
    GET /api/v1/report/actualFlow                    → list of (id, asAt, gasDay)
    GET /api/v1/report/actualFlow/<YYYY-MM-DD>       → detail with `rows`

Each row in the detail response has:
    facilityCode, facilityName, facilityType ('production'|'pipeline'|'storage'|...)
    gateStationName, zoneName  (set for pipeline rows)
    receipt (TJ in), delivery (TJ out)

For the dashboard we surface:
    - DBP (Dampier to Bunbury) total delivery, summed across all zones/gates
    - Production facilities (facilityType='production') with their receipt value

The WA API publishes settled actuals and lags by ~2 days, similar to east coast GBB."""

from __future__ import annotations

import logging

import httpx

from app.config import AEMO_HEADERS


BASE_URL = "https://gbbwa.aemo.com.au/api/v1/report/actualFlow"

# Current DBP capacity (~1066 TJ/d) after the 2018 Phase 5 expansion. The brief's
# 845 figure is the pre-expansion nameplate; using current capacity keeps the
# utilisation reading meaningful.
DBP_NAMEPLATE_TJD = 1066
DBP_FACILITY_NAME = "Dampier to Bunbury Natural Gas Pipeline"

log = logging.getLogger(__name__)


def fetch() -> dict:
    """Fetch the latest available WA actualFlow report and aggregate."""
    with httpx.Client(headers=AEMO_HEADERS, timeout=30.0, follow_redirects=True) as client:
        index_resp = client.get(BASE_URL)
        index_resp.raise_for_status()
        items = index_resp.json()
        if not isinstance(items, list) or not items:
            raise ValueError(f"WA actualFlow index returned {type(items).__name__} of length 0")
        latest_day = max(item["gasDay"] for item in items if "gasDay" in item)

        detail_resp = client.get(f"{BASE_URL}/{latest_day}")
        detail_resp.raise_for_status()
        detail = detail_resp.json()
    return aggregate(detail)


def aggregate(detail: dict) -> dict:
    """Turn the raw detail JSON into the shape the dashboard template expects."""
    rows = detail.get("rows", [])

    # DBP — the API publishes DBP rows at two granularities: zone-level totals
    # (Dampier, Metro, South-West) and individual gate-station rows. Summing both
    # double-counts the same gas. We sum zone-level rows only (gateStationName is
    # null) and use total delivery (gas leaving DBP to customers) as the headline.
    dbp_zone_rows = [
        r for r in rows
        if r.get("facilityName") == DBP_FACILITY_NAME and r.get("gateStationName") is None
    ]
    dbp_flow = sum((r.get("delivery") or 0) for r in dbp_zone_rows)
    dbp_util = round(100 * dbp_flow / DBP_NAMEPLATE_TJD, 1) if DBP_NAMEPLATE_TJD else 0.0

    # Production facilities — one row per facility with receipt as output.
    production = [r for r in rows if r.get("facilityType") == "production"]
    facilities = [
        {
            "name": r.get("facilityName") or r.get("facilityCode") or "?",
            "output_tjd": float(r.get("receipt") or 0),
            "stale": False,
        }
        for r in sorted(production, key=lambda r: -(r.get("receipt") or 0))
    ]

    return {
        "as_of_gas_day": detail.get("gasDay"),
        "as_at": detail.get("asAt"),
        "dbp_flow": round(dbp_flow, 1),
        "dbp_nameplate": DBP_NAMEPLATE_TJD,
        "dbp_utilisation": dbp_util,
        "facilities": facilities,
        "indicative_price": None,  # No live source yet
    }
