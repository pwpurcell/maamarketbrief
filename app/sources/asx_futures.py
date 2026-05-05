"""ASX Energy public futures dataset.

Source: https://www.asxenergy.com.au/futures_au/dataset (electricity)
        https://www.asxenergy.com.au/futures_gas/dataset (gas)

ASX Energy is ASX's public-facing site for ASX 24 energy derivatives. The
two `/dataset` endpoints return rendered HTML tables (delayed ~20 min) that
power the futures pages at /futures_au and /futures_gas. We scrape the
tables and surface bid/ask/last/settle/vol/open-interest per contract.

Public delayed data — fine for a personal/internal dashboard. The site's
TOS prohibits redistribution, so don't expose the dashboard publicly without
review."""

from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup


HEADERS = {"User-Agent": "Mozilla/5.0 (markets-brief; non-commercial)"}
ELEC_URL = "https://www.asxenergy.com.au/futures_au/dataset"
GAS_URL = "https://www.asxenergy.com.au/futures_gas/dataset"

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=HEADERS, timeout=30.0, follow_redirects=True) as client:
        elec = client.get(ELEC_URL); elec.raise_for_status()
        gas = client.get(GAS_URL); gas.raise_for_status()
    elec_data = parse(elec.text)
    gas_data = parse(gas.text)
    return {
        "as_of_gas_day": elec_data.get("date") or gas_data.get("date") or "",
        "as_of_time": elec_data.get("time") or gas_data.get("time") or "",
        "elec_sections": elec_data["sections"],
        "gas_sections": gas_data["sections"],
    }


_CODE_RE = re.compile(r"^[A-Z]{2,5}[FGHJKMNQUVXZ]\d{2,4}$")
# Second letter of the ASX 24 electricity contract code encodes the region.
STATE_FROM_CODE_LETTER = {"N": "NSW", "V": "VIC", "Q": "QLD", "S": "SA"}


def parse(html: str) -> dict:
    """Walk every leaf data table on the dataset and extract its rows.

    For electricity the page uses a layout table to lay NSW/VIC/QLD/SA out
    in columns; `find_previous("h2")` always returns "South Australia" (the
    last h2 in document order), so we instead derive the state from the
    contract code's second letter. For gas, contracts use distinct prefixes
    (GX/GY/GZ) so the h2 heading is reliable."""
    soup = BeautifulSoup(html, "html.parser")
    last = soup.select_one("#last")
    date_str = last.get("data-date", "") if last else ""
    time_str = last.get("data-time", "") if last else ""

    sections: list[dict] = []
    for table in soup.find_all("table"):
        # Skip outer/layout tables that contain nested data tables.
        if table.find("table"):
            continue
        rows = _extract_rows(table)
        if not rows:
            continue
        h3 = table.find_previous("h3")
        h2 = table.find_previous("h2")
        sub = _clean_heading(h3.get_text(" ", strip=True)) if h3 else ""
        h2_text = _clean_heading(h2.get_text(" ", strip=True)) if h2 else ""

        # Derive electricity region from contract code; falls back to h2 for gas.
        first_code = rows[0]["code"]
        state = STATE_FROM_CODE_LETTER.get(first_code[1]) if len(first_code) >= 2 else None
        if state and sub:
            section = f"{state} — {sub}"
        elif state:
            section = state
        else:
            section = h2_text or sub or "Unknown"
        sections.append({"section": section, "rows": rows})

    return {"date": date_str, "time": time_str, "sections": sections}


def _clean_heading(text: str) -> str:
    # Strip trailing 2-letter ASX product code (e.g. "Victoria Gas Quarterly GX" → "Victoria Gas Quarterly").
    return re.sub(r"\s+[A-Z]{2}$", "", text).strip()


def _extract_rows(table) -> list[dict]:
    """Map each <tbody> row's cells onto the <thead> column names. Returns []
    if the table doesn't have a "settle" header (i.e. it's a layout table)."""
    head = table.find("thead")
    if not head:
        return []
    head_row = head.find("tr")
    if not head_row:
        return []
    headers = [td.get_text(strip=True).lower() for td in head_row.find_all(["th", "td"])]
    if "settle" not in headers:
        return []

    rows: list[dict] = []
    for tr in table.find_all("tr"):
        if tr.find_parent("thead"):
            continue
        cells = tr.find_all("td")
        if not cells:
            continue
        first = cells[0]
        if "instrument" not in (first.get("class") or []):
            continue
        label = first.get_text(strip=True)
        code = (first.get("title") or label).strip()
        # Skip degenerate rows where code isn't a real contract code.
        if not _CODE_RE.match(code):
            continue
        values: dict[str, str] = {}
        for header, cell in zip(headers, cells):
            if header == "instrument" or not header:
                continue
            values[header] = cell.get_text(strip=True)
        rows.append({"label": label, "code": code, **values})
    return rows
