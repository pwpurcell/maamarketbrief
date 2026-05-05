"""Gas storage facilities — Iona, Dandenong, Newcastle, Roma, Silver Springs, Moomba.

Source: AEMO GBB GasBBActualFlowStorage.zip — the same file structure we used
for east-coast pipeline actuals before we switched the pipelines section to
nominations. Storage facilities (FacilityType=STOR) carry a HeldInStorage
column with total inventory in TJ. The file is settled and lags by ~1-2 days.

Working capacities are hard-coded per facility from public AEMO/operator
disclosures (GSOO, operator annual reports). Update when new expansions
are commissioned."""

from __future__ import annotations

import io
import logging
import zipfile
from io import StringIO

import httpx
import pandas as pd

from app.config import AEMO_HEADERS


URL = "https://nemweb.com.au/Reports/Current/GBB/GasBBActualFlowStorage.zip"

# Headline storage capacity in PJ (= 1000 TJ) — cross-checked against AEMO
# GBB Nameplate Rating file 2026-05-05. AEMO's STORAGE figure includes
# cushion gas for depleted-field facilities, hence the larger numbers for
# RUGS / SSSF / Moomba vs operator-quoted "working gas" figures.
WORKING_CAPACITY_PJ = {
    "Iona UGS":       28.0,   # Lochard post-2024 expansion (AEMO file still shows 24.4 PJ effective Mar 2024)
    "Dandenong LNG":   0.68,  # AEMO Nameplate, Dandenong LNG STORAGE 680.8 TJ
    "NGS":             1.5,   # AEMO Nameplate, NGS STORAGE 1500 TJ
    "RUGS":           54.0,   # AEMO Nameplate (incl cushion); operator quotes ~14-20 PJ working
    "SSSF":           45.0,   # AEMO Nameplate (incl cushion)
    "Moomba Storage": 70.0,   # AEMO Nameplate (incl cushion in depleted Cooper Basin reservoirs)
}

# Cushion gas — minimum inventory below which the facility can't physically
# withdraw. Subtracted from inventory before computing days of cover.
# Iona's 6 PJ floor comes from the 2023 GSOO; other facilities aren't published
# in the same level of detail, so cushion = 0 (working gas = full inventory).
CUSHION_GAS_PJ = {
    "Iona UGS":       6.0,    # 2023 GSOO minimum operable storage level
    "Dandenong LNG":  0.0,
    "NGS":            0.0,
    "RUGS":           0.0,
    "SSSF":           0.0,
    "Moomba Storage": 0.0,
}

# Maximum daily withdrawal rate (TJ/d) — AEMO Nameplate MDQ values (2026-05-05).
# Iona uses the SWP-bound effective rate (515 TJ/d) since Iona's facility
# MDQ of 570 TJ/d is constrained by the South West Pipeline downstream.
# Moomba uses 250 TJ/d as a Cooper Basin cycle approximation; AEMO's published
# 4.9 TJ/d MDQ is for dedicated storage withdrawal only and isn't the practical
# system rate when Santos draws from Cooper Basin reservoirs.
MAX_WITHDRAWAL_TJD = {
    "Iona UGS":       515,    # AEMO MDQ 570; SWP carries 515 — system-binding figure
    "Dandenong LNG":  237,    # AEMO MDQ 237.2
    "NGS":            120,    # AEMO MDQ 120
    "RUGS":           150,    # AEMO MDQ 150
    "SSSF":            25,    # AEMO MDQ 25 (reservoir-engineering constrained)
    "Moomba Storage": 250,    # Cooper Basin cycle approximation; AEMO MDQ of 4.9 TJ/d is dedicated-storage only
}

DISPLAY_NAME = {
    "Iona UGS":       "Iona UGS",
    "Dandenong LNG":  "Dandenong LNG",
    "NGS":            "Newcastle Gas Storage",
    "RUGS":           "Roma Underground (Origin)",
    "SSSF":           "Silver Springs (QGC)",
    "Moomba Storage": "Moomba (Santos)",
}

REGION_NOTES = {
    "Iona UGS":       "Southern system / VTS",
    "Dandenong LNG":  "Victoria peaking",
    "NGS":            "NSW peaking",
    "RUGS":           "Wallumbilla / SE Qld",
    "SSSF":           "Wallumbilla / SE Qld",
    "Moomba Storage": "Cooper Basin / SA, NSW, QLD",
}

log = logging.getLogger(__name__)


def fetch() -> dict:
    with httpx.Client(headers=AEMO_HEADERS, timeout=60.0, follow_redirects=True) as client:
        resp = client.get(URL)
        resp.raise_for_status()
    return aggregate(resp.content)


def aggregate(zip_bytes: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        if not csv_names:
            raise ValueError("GBB ActualFlowStorage zip has no CSV")
        text = zf.read(csv_names[0]).decode("utf-8", errors="replace")
    df = pd.read_csv(StringIO(text))
    df.columns = [c.strip() for c in df.columns]
    df["GasDate"] = pd.to_datetime(df["GasDate"], format="%Y/%m/%d", errors="coerce")
    df = df.dropna(subset=["GasDate"])

    latest = df["GasDate"].max()
    today_df = df[(df["GasDate"] == latest) & (df["FacilityType"] == "STOR")]

    rows = []
    for facility, working_pj in WORKING_CAPACITY_PJ.items():
        fac_rows = today_df[today_df["FacilityName"] == facility]
        if fac_rows.empty:
            continue
        held_tj = pd.to_numeric(fac_rows["HeldInStorage"], errors="coerce").sum()
        if pd.isna(held_tj) or held_tj <= 0:
            continue
        held_pj = float(held_tj) / 1000.0

        supply = pd.to_numeric(fac_rows["Supply"], errors="coerce").fillna(0).sum()
        demand = pd.to_numeric(fac_rows["Demand"], errors="coerce").fillna(0).sum()
        # Convention: positive net flow = injection (gas going IN to storage),
        # negative = withdrawal. AEMO publishes demand=withdrawal at storage,
        # supply=injection from storage to network — so we invert the sign.
        # i.e. when "supply" > 0 storage is delivering (withdrawing); when
        # "demand" > 0 storage is taking gas in (injecting).
        # Hmm actually the convention in the GBB is opposite to my first read.
        # Looking at Iona on a cold winter day: Demand=56 (storage receiving
        # withdrawal nominations from VTS = gas taken OUT of storage),
        # Supply=6 (small injection back). Net flow positive into storage =
        # supply - demand = -50 (i.e. net withdrawal of 50 TJ). Map to
        # display convention: positive = injection, negative = withdrawal.
        net_flow_tjd = float(supply - demand)

        # Days of cover = (operable inventory) / max withdrawal rate.
        # Operable = HeldInStorage − cushion gas (gas physically immobile below
        # the minimum operable storage level). For Iona that's 6 PJ per the
        # 2023 GSOO; for facilities without published cushion data we use 0.
        # This is the planning metric: "if this facility runs at peak to
        # support the system, how long until effectively empty?".
        max_withdrawal = MAX_WITHDRAWAL_TJD.get(facility)
        cushion_tj = CUSHION_GAS_PJ.get(facility, 0.0) * 1000
        operable_tj = max(0.0, float(held_tj) - cushion_tj)
        days_cover = round(operable_tj / max_withdrawal, 1) if max_withdrawal else None

        if net_flow_tjd > 0.5:
            arrow, cls = "↑", "up"
        elif net_flow_tjd < -0.5:
            arrow, cls = "↓", "down"
        else:
            arrow, cls = "→", "flat"

        rows.append({
            "facility": DISPLAY_NAME[facility],
            "inventory_pj": round(held_pj, 1),
            "capacity_pj": working_pj,
            "pct_full": round(100 * held_pj / working_pj, 1) if working_pj else None,
            "net_flow_tjd": round(net_flow_tjd, 1),
            "net_flow_arrow": arrow,
            "net_flow_class": cls,
            "days_cover": days_cover,
            "region": REGION_NOTES[facility],
            "stale": False,
        })

    return {
        "as_of_gas_day": latest.date().isoformat(),
        "rows": rows,
    }
