"""Static configuration: pipeline metadata, fuel categories, env vars.

Pipeline nameplates here are sourced from the AEMO GSOO and the most recent
GBB facility lists. Update when AEMO publishes a new GSOO.

GBB column semantics (verified 2026-05 against GasBBActualFlowStorage.csv):
    Supply       = gas the pipeline received at LocationName from a field/supplier (gas IN)
    Demand       = gas the pipeline delivered at LocationName to customers      (gas OUT)
    TransferIn   = gas received from another pipe at this location              (gas IN)
    TransferOut  = gas delivered to another pipe at this location               (gas OUT)
Flow on a pipeline ≈ total Inflow ≈ total Outflow (modulo linepack)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class PipelineSpec:
    id: str
    display: str
    nameplate_tjd: int
    direction: str = ""
    # GBB ActualFlowStorage matching:
    facility: str | None = None              # exact match on FacilityName (case-insensitive); None means no GBB data
    location: str | None = None              # optional case-insensitive substring on LocationName
    flow_columns: tuple[str, ...] = ()       # columns to sum for the flow value (e.g. ("Demand","TransferOut"))


# Pipeline nameplates cross-checked against AEMO GBB Nameplate Rating file
# (current effective values, fact-checked 2026-05-05). Order matches the
# dashboard table layout. Per-pipeline flow rules picked to give a single
# defensible TJ/d number against the real GBB nominations CSV.
PIPELINES: list[PipelineSpec] = [
    # SWP isn't its own GBB facility; capacity is published in VTS as the
    # Iona Hub injection → Brooklyn route (515 TJ/d effective Mar 2026).
    PipelineSpec("swp",    "South West Pipeline (VIC)",      515, "Iona to VTS (TransferIn at VTS Iona Hub)",
                 facility="VTS",  location="Iona",        flow_columns=("TransferIn",)),

    PipelineSpec("maps",   "Moomba to Adelaide (MAPS)",      252, "Moomba Inj. to Pelican Point",
                 facility="MAPS", location=None,          flow_columns=("Demand", "TransferOut")),

    # SWQP bidirectional — measured at Wallumbilla (eastern end choke point).
    # Eastbound = Moomba → Wallumbilla via "Moomba Delivery Stream → SWQP to RCWP".
    PipelineSpec("swqp_e", "SWQP (eastbound)",               340, "Eastbound at Wallumbilla",
                 facility="SWQP", location="Wallumbilla",  flow_columns=("TransferOut",)),
    # Westbound = Wallumbilla → Moomba via "SWQP from RCWP → SWQP MAPS Delivery".
    PipelineSpec("swqp_w", "SWQP (westbound)",               512, "Westbound at Wallumbilla",
                 facility="SWQP", location="Wallumbilla",  flow_columns=("TransferIn",)),

    PipelineSpec("egp",    "Eastern Gas Pipeline (EGP)",     349, "Longford to Horsley Park (Sydney)",
                 facility="EGP",  location=None,           flow_columns=("Demand", "TransferOut")),

    PipelineSpec("msp",    "Moomba Sydney Pipeline (MSP)",   565, "Moomba Inlet to Wilton",
                 facility="MSP",  location=None,           flow_columns=("Demand", "TransferOut")),

    PipelineSpec("rbp",    "Roma Brisbane Pipeline (RBP)",   167, "Wallumbilla to Gibson Island (Brisbane)",
                 facility="RBP",  location=None,           flow_columns=("Demand", "TransferOut")),
]


# AEMO blocks default Python and curl User-Agents. Set on every request.
AEMO_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


NEM_REGIONS: list[str] = ["QLD1", "NSW1", "VIC1", "SA1", "TAS1"]


# Display order for the generation mix table.
FUEL_CATEGORIES: list[str] = [
    "Black coal",
    "Brown coal",
    "Gas",
    "Hydro",
    "Wind",
    "Utility solar",
    "Rooftop solar (estimated)",
    "Battery",
    "Diesel",
    "Biomass / other",
]


# Source: AEMO Generation Information page / latest GSOO. Update annually.
ROOFTOP_PV_INSTALLED_MW: int = 24_000


TIMEZONE = os.getenv("TIMEZONE", "Australia/Melbourne")
REFRESH_INTERVAL_MINUTES = int(os.getenv("REFRESH_INTERVAL_MINUTES", "15"))
