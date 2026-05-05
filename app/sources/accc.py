"""ACCC LNG netback price series — monthly published Excel.

Source page: https://www.accc.gov.au/inquiries-and-consultations/gas-inquiry-2017-30/lng-netback-price-series

The page links to a monthly Excel file (filename includes the publication
month, e.g. "LNG netback price series - Public version - 1 May.xlsx") which
gets republished around the 1st of each month. We scrape the page to find
the current filename, download the Excel, and read the "JKM Netback Chart
Data" sheet for the most recent historical monthly netback in AUD/GJ.

Data lags by ~1 month (May value published in early May for April actuals)."""

from __future__ import annotations

import io
import logging
import re
from urllib.parse import urljoin

import httpx
import pandas as pd


PAGE_URL = "https://www.accc.gov.au/inquiries-and-consultations/gas-inquiry-2017-30/lng-netback-price-series"
HEADERS = {"User-Agent": "Mozilla/5.0 (markets-brief; non-commercial)"}

log = logging.getLogger(__name__)


def fetch() -> dict:
    """Scrape the ACCC page for the current Excel link, download, return the latest monthly netback."""
    with httpx.Client(headers=HEADERS, timeout=60.0, follow_redirects=True) as client:
        page = client.get(PAGE_URL)
        page.raise_for_status()
        match = re.search(r'href="([^"]*LNG[^"]*netback[^"]*\.xlsx)"', page.text, re.IGNORECASE)
        if not match:
            raise ValueError("No LNG netback xlsx link on ACCC page")
        href = match.group(1)
        xlsx_url = urljoin(PAGE_URL, href)
        log.info("ACCC netback xlsx: %s", xlsx_url)
        xlsx_resp = client.get(xlsx_url)
        xlsx_resp.raise_for_status()
    return aggregate(xlsx_resp.content)


def aggregate(xlsx_bytes: bytes) -> dict:
    df = pd.read_excel(io.BytesIO(xlsx_bytes), sheet_name="JKM Netback Chart Data", engine="openpyxl", header=3)
    df = df.rename(columns={df.columns[1]: "delivery_month", df.columns[2]: "historical_netback"})
    df = df[["delivery_month", "historical_netback"]].dropna(subset=["delivery_month"])
    df = df[df["historical_netback"].notna()]
    if df.empty:
        raise ValueError("ACCC netback Excel: no historical netback values found")

    df["delivery_month"] = pd.to_datetime(df["delivery_month"], errors="coerce")
    df = df.dropna(subset=["delivery_month"]).sort_values("delivery_month")
    latest_row = df.iloc[-1]

    return {
        "as_of_gas_day": latest_row["delivery_month"].strftime("%Y-%m-%d"),
        "month_label": latest_row["delivery_month"].strftime("%b %Y"),
        "netback_aud_per_gj": round(float(latest_row["historical_netback"]), 2),
        "history": [
            {
                "month": r["delivery_month"].strftime("%Y-%m"),
                "value": round(float(r["historical_netback"]), 2),
            }
            for _, r in df.tail(12).iterrows()
        ],
    }
