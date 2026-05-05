"""NEM 5-minute interconnector flows from DispatchIS INTERCONNECTORRES.

Source: same DispatchIS zip as `app.sources.nem` (dispatch RRP). We re-parse
the INTERCONNECTORRES table for the latest 5-minute snapshot.

Convention:
    INTERCONNECTORID encodes from→to (e.g. "VIC1-NSW1" = positive flow VIC→NSW).
    MWFLOW > 0 → flow from "from" to "to". MWFLOW < 0 → reverse.
    EXPORTLIMIT / IMPORTLIMIT are AEMO's published real-time limits (TNUOS,
    constraint-set, system normal — whichever is binding right now).

The display picks the binding direction limit (export when flowing positive,
import when flowing negative) for the utilisation calculation."""

from __future__ import annotations

import io
import logging
import re
import zipfile

import httpx
import pandas as pd

from app.config import AEMO_HEADERS
from app.sources._nemweb import parse_aemo_idr_csv


BASE_URL = "https://nemweb.com.au/Reports/Current/DispatchIS_Reports/"
FILENAME_RE = re.compile(r"PUBLIC_DISPATCHIS_\d+_\d+\.zip")

# Display metadata. Order = display order in the dashboard.
INTERCONNECTORS = [
    {"id": "NSW1-QLD1", "display": "QNI",         "from_region": "NSW1", "to_region": "QLD1", "kind": "AC"},
    {"id": "N-Q-MNSP1", "display": "Terranora",   "from_region": "NSW1", "to_region": "QLD1", "kind": "AC"},
    {"id": "VIC1-NSW1", "display": "VNI",         "from_region": "VIC1", "to_region": "NSW1", "kind": "AC"},
    {"id": "V-SA",      "display": "Heywood",     "from_region": "VIC1", "to_region": "SA1",  "kind": "AC"},
    {"id": "V-S-MNSP1", "display": "Murraylink",  "from_region": "VIC1", "to_region": "SA1",  "kind": "DC"},
    {"id": "T-V-MNSP1", "display": "Basslink",    "from_region": "TAS1", "to_region": "VIC1", "kind": "DC"},
]

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=AEMO_HEADERS, timeout=60.0, follow_redirects=True) as client:
        listing = client.get(BASE_URL)
        listing.raise_for_status()
        files = FILENAME_RE.findall(listing.text)
        if not files:
            raise ValueError("No PUBLIC_DISPATCHIS_*.zip files found")
        latest = sorted(files)[-1]
        zip_resp = client.get(BASE_URL + latest)
        zip_resp.raise_for_status()
    df = _parse_zip(zip_resp.content)
    return _aggregate(df)


def _parse_zip(content: bytes) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError(f"DispatchIS zip contains no CSV (members: {zf.namelist()})")
        text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
    return parse_aemo_idr_csv(text, "INTERCONNECTORRES")


def _aggregate(df: pd.DataFrame) -> dict:
    df = df.copy()
    df["INTERVENTION"] = pd.to_numeric(df["INTERVENTION"], errors="coerce").fillna(-1).astype(int)
    df = df[df["INTERVENTION"] == 0]
    df["SETTLEMENTDATE"] = pd.to_datetime(df["SETTLEMENTDATE"], errors="coerce")
    df["MWFLOW"] = pd.to_numeric(df["MWFLOW"], errors="coerce")
    df["EXPORTLIMIT"] = pd.to_numeric(df["EXPORTLIMIT"], errors="coerce")
    df["IMPORTLIMIT"] = pd.to_numeric(df["IMPORTLIMIT"], errors="coerce")
    df = df.dropna(subset=["SETTLEMENTDATE", "MWFLOW", "INTERCONNECTORID"])

    rows: list[dict] = []
    for spec in INTERCONNECTORS:
        ic_df = df[df["INTERCONNECTORID"] == spec["id"]]
        if ic_df.empty:
            continue
        latest = ic_df.sort_values("SETTLEMENTDATE", ascending=False).iloc[0]
        mw = float(latest["MWFLOW"])
        export_lim = float(latest["EXPORTLIMIT"]) if pd.notna(latest["EXPORTLIMIT"]) else None
        import_lim = float(latest["IMPORTLIMIT"]) if pd.notna(latest["IMPORTLIMIT"]) else None

        if mw >= 0:
            direction_text = f"{spec['from_region']} → {spec['to_region']}"
            limit_used = export_lim
            arrow = "↑"
            arrow_class = "up"
        else:
            direction_text = f"{spec['to_region']} → {spec['from_region']}"
            limit_used = import_lim
            arrow = "↓"
            arrow_class = "down"

        abs_mw = abs(mw)
        # AEMO publishes IMPORTLIMIT as a negative number (it's a constraint
        # on the negative-flow direction). Use abs() for both magnitude display
        # and the utilisation ratio.
        abs_limit = abs(limit_used) if limit_used is not None else None
        util = round(100 * abs_mw / abs_limit, 1) if abs_limit and abs_limit > 0 else None

        rows.append({
            "id": spec["id"],
            "display": spec["display"],
            "kind": spec["kind"],
            "direction_text": direction_text,
            "mw_flow": round(abs_mw, 1),
            "mw_flow_signed": round(mw, 1),
            "export_limit": round(abs(export_lim), 0) if export_lim is not None else None,
            "import_limit": round(abs(import_lim), 0) if import_lim is not None else None,
            "limit_used": round(abs_limit, 0) if abs_limit is not None else None,
            "utilisation": util,
            "arrow": arrow,
            "arrow_class": arrow_class,
            "stale": False,
        })

    return {
        "as_of": df["SETTLEMENTDATE"].max().isoformat() if not df.empty else None,
        "rows": rows,
    }
