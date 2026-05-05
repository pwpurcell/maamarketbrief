"""Macro context — FX, interest rates, global commodity references.

All sources are free public CSVs (no API keys):

    AUD/USD daily       → RBA F11.1 (Series ID FXRUSD)
    RBA cash rate (M)   → RBA F1.1  (Series ID FIRMMCRT)
    US 10y Treasury (D) → FRED DGS10
    Brent (D, ~3-7d lag) → FRED DCOILBRENTEU
    Henry Hub (D, ~3-7d lag) → FRED DHHNGSP

Returns one combined snapshot used by the Macro Context section's FX/rates
cards and the LNG Netback section's Global Reference cards (Brent + Henry Hub).
JKM and TTF are deliberately omitted — no free public source. Paul will
organise a paid feed for those separately.

AU 10y bond yield was previously included via RBA F2.1 monthly but dropped
2026-05-05 — AOFM publishes it daily and would be a better source if it
ever returns to the dashboard."""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging

import httpx


HEADERS = {"User-Agent": "Mozilla/5.0 (markets-brief; non-commercial)"}

RBA_F11_URL = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"
RBA_F1_URL = "https://www.rba.gov.au/statistics/tables/csv/f1.1-data.csv"
FRED_TEMPLATE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=HEADERS, timeout=60.0, follow_redirects=True) as client:
        audusd, audusd_prev = _rba_series(client, RBA_F11_URL, "FXRUSD")
        cash_rate, _ = _rba_series(client, RBA_F1_URL, "FIRMMCRT")
        us10y, us10y_prev = _fred_series(client, "DGS10")
        brent, _ = _fred_series(client, "DCOILBRENTEU")
        henry_hub, _ = _fred_series(client, "DHHNGSP")

    return {
        "as_of_gas_day": dt.date.today().isoformat(),
        "fx_rates": [
            _fx_card("AUD/USD", audusd, audusd_prev, "{:.4f}", "Daily, RBA F11.1"),
            _rate_card("RBA cash rate target", cash_rate, "Monthly, RBA F1.1"),
            _delta_rate_card("US 10y Treasury", us10y, us10y_prev, "Daily, FRED DGS10"),
        ],
        "global_lng": [
            _commodity_card("Brent front-month", brent, "USD/bbl", "Daily, FRED DCOILBRENTEU (~3-7d lag)"),
            _commodity_card("Henry Hub front-month", henry_hub, "USD/MMBtu", "Daily, FRED DHHNGSP (~3-7d lag)"),
        ],
    }


def _rba_series(client: httpx.Client, url: str, series_id: str) -> tuple[float | None, float | None]:
    """Pull the latest two values for a series_id from an RBA F-table CSV."""
    resp = client.get(url)
    resp.raise_for_status()
    return _parse_rba(resp.text, series_id)


def _parse_rba(text: str, series_id: str) -> tuple[float | None, float | None]:
    rows = list(csv.reader(io.StringIO(text)))
    # Find the row that begins with "Series ID" — gives the column index.
    series_row_idx = next((i for i, row in enumerate(rows) if row and row[0] == "Series ID"), None)
    if series_row_idx is None:
        raise ValueError("RBA CSV missing 'Series ID' header row")
    series_row = rows[series_row_idx]
    try:
        col = series_row.index(series_id)
    except ValueError:
        raise ValueError(f"Series {series_id!r} not in RBA CSV") from None

    values: list[float] = []
    for row in rows[series_row_idx + 1:]:
        if not row or not row[0].strip():
            continue
        if len(row) <= col:
            continue
        cell = row[col].strip()
        if not cell:
            continue
        try:
            values.append(float(cell))
        except ValueError:
            continue

    if not values:
        return None, None
    if len(values) == 1:
        return values[-1], None
    return values[-1], values[-2]


def _fred_series(client: httpx.Client, series_id: str) -> tuple[float | None, float | None]:
    resp = client.get(FRED_TEMPLATE.format(series_id))
    resp.raise_for_status()
    return _parse_fred(resp.text, series_id)


def _parse_fred(text: str, series_id: str) -> tuple[float | None, float | None]:
    reader = csv.DictReader(io.StringIO(text))
    values: list[float] = []
    for row in reader:
        v = (row.get(series_id) or "").strip()
        if not v or v == ".":
            continue
        try:
            values.append(float(v))
        except ValueError:
            continue
    if not values:
        return None, None
    if len(values) == 1:
        return values[-1], None
    return values[-1], values[-2]


def _fx_card(label: str, current: float | None, prev: float | None, fmt: str, context: str) -> dict:
    if current is None:
        return {"label": label, "value_text": "—", "delta_text": "", "context": context, "tone_class": "flat"}
    delta = current - prev if prev is not None else None
    delta_text, cls = _delta_signage(delta, fmt="{:+.4f}")
    return {"label": label, "value_text": fmt.format(current), "delta_text": delta_text or "", "context": context, "tone_class": cls}


def _rate_card(label: str, current: float | None, context: str) -> dict:
    if current is None:
        return {"label": label, "value_text": "—", "delta_text": "", "context": context, "tone_class": "flat"}
    return {"label": label, "value_text": f"{current:.2f}%", "delta_text": "", "context": context, "tone_class": "flat"}


def _delta_rate_card(label: str, current: float | None, prev: float | None, context: str) -> dict:
    if current is None:
        return {"label": label, "value_text": "—", "delta_text": "", "context": context, "tone_class": "flat"}
    delta = (current - prev) if prev is not None else None
    delta_text, cls = _delta_signage(delta, fmt="{:+.2f} pp")
    return {"label": label, "value_text": f"{current:.2f}%", "delta_text": delta_text or "", "context": context, "tone_class": cls}


def _commodity_card(series: str, current: float | None, unit: str, context: str) -> dict:
    if current is None:
        return {"series": series, "value_main": None, "unit_main": unit, "value_alt": None, "unit_alt": None, "delta": None, "delta_arrow": "", "delta_class": "flat", "context": context, "stale": True}
    return {"series": series, "value_main": current, "unit_main": unit, "value_alt": None, "unit_alt": None, "delta": None, "delta_arrow": "", "delta_class": "flat", "context": context, "stale": False}


def _delta_signage(delta: float | None, fmt: str) -> tuple[str, str]:
    if delta is None:
        return "", "flat"
    if delta == 0:
        return fmt.format(0), "flat"
    cls = "up" if delta > 0 else "down"
    return fmt.format(delta), cls
