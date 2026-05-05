"""Parser tests against committed AEMO sample files in tests/fixtures/.

Sample files are real AEMO outputs fetched once on 2026-05-04 and committed
so the parsers stay testable when AEMO endpoints change shape."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.sources import gbb, sttm, dwgm, gsh, nem, duid_registry, dispatch_unit, rooftop_pv, genmix, wagbb


FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def gbb_zip_bytes() -> bytes:
    return (FIXTURE_DIR / "GasBBNominationAndForecast.zip").read_bytes()


# Fixed snapshot date so tests are deterministic against the committed fixture.
FIXTURE_GAS_DAY = pd.Timestamp("2026-05-04")


def test_gbb_parse_zip_returns_normalised_frame(gbb_zip_bytes):
    df = gbb._parse_zip(gbb_zip_bytes)
    assert {"Gasdate", "FacilityName", "Demand", "Supply", "TransferIn", "TransferOut"}.issubset(df.columns)
    assert df["Gasdate"].max().year >= 2026
    assert (df["FacilityName"] == "SWQP").any()


def test_gbb_aggregate_returns_one_row_per_pipeline(gbb_zip_bytes):
    df = gbb._parse_zip(gbb_zip_bytes)
    result = gbb._aggregate(df, today=FIXTURE_GAS_DAY)

    assert result["stale"] is False
    assert result["as_of_gas_day"] == "2026-05-04"

    by_id = {row["id"]: row for row in result["pipelines"]}
    assert set(by_id) == {"swp", "maps", "swqp_e", "swqp_w", "egp", "msp", "rbp"}

    for pid, row in by_id.items():
        assert isinstance(row["flow"], float), f"{pid} flow should be float, got {row['flow']!r}"
        assert row["flow"] >= 0, f"{pid} flow should be non-negative"
        assert row["utilisation"] >= 0


def test_gbb_aggregate_plausible_pipeline_flows(gbb_zip_bytes):
    """Wide-bracket sanity check on computed flows for the fixture's gas day."""
    df = gbb._parse_zip(gbb_zip_bytes)
    by_id = {row["id"]: row for row in gbb._aggregate(df, today=FIXTURE_GAS_DAY)["pipelines"]}

    assert 0 < by_id["maps"]["flow"] < 250
    assert 0 < by_id["egp"]["flow"] < 400
    assert 0 <= by_id["swqp_e"]["flow"] < 400
    assert 0 <= by_id["swqp_w"]["flow"] < 450


def test_gbb_aggregate_delta_arrows_classified(gbb_zip_bytes):
    df = gbb._parse_zip(gbb_zip_bytes)
    rows = gbb._aggregate(df, today=FIXTURE_GAS_DAY)["pipelines"]
    arrows = {row["delta_arrow"] for row in rows}
    assert arrows.issubset({"↑", "↓", "→"})


def test_gbb_aggregate_falls_back_when_target_day_missing(gbb_zip_bytes):
    df = gbb._parse_zip(gbb_zip_bytes)
    # Pick a future date that won't be in the file (10 years ahead).
    future = pd.Timestamp("2036-01-01")
    result = gbb._aggregate(df, today=future)
    # Should have fallen back to the latest published nomination day.
    assert result["as_of_gas_day"] is not None


# --- STTM ---------------------------------------------------------------------

def test_sttm_parse_and_aggregate():
    text = (FIXTURE_DIR / "sttm_int651_ex_ante_market_price.csv").read_text(encoding="utf-8")
    df = sttm._parse_csv(text)
    result = sttm._aggregate(df)
    by_hub = {row["hub"]: row for row in result["prices"]}
    assert set(by_hub) == {"STTM Sydney", "STTM Adelaide", "STTM Brisbane"}
    for hub, row in by_hub.items():
        assert isinstance(row["today"], float)
        assert 0 < row["today"] < 100, f"{hub} price out of plausible range"
        assert row["stale"] is False


# --- DWGM ---------------------------------------------------------------------

def test_dwgm_parse_and_aggregate():
    text = (FIXTURE_DIR / "dwgm_int041_market_and_reference_prices.csv").read_text(encoding="utf-8")
    df = dwgm._parse_csv(text)
    result = dwgm._aggregate(df)
    assert len(result["prices"]) == 1
    row = result["prices"][0]
    assert row["hub"] == "DWGM (6am)"
    assert isinstance(row["today"], float) and 0 < row["today"] < 100
    assert "as_of_gas_day" in result


# --- GSH ----------------------------------------------------------------------

def test_gsh_parse_and_aggregate():
    zip_bytes = (FIXTURE_DIR / "gsh_wallumbilla_benchmark.zip").read_bytes()
    df = gsh._parse_zip(zip_bytes)
    assert {"GAS_DATE", "PRODUCT_LOCATION", "BENCHMARK_PRICE", "IS_FIRM"}.issubset(df.columns)
    assert (df["PRODUCT_LOCATION"] == "WAL").any()
    result = gsh._aggregate(df)
    row = result["prices"][0]
    assert row["hub"] == "Wallumbilla GSH"
    assert isinstance(row["today"], float) and 0 < row["today"] < 100


# --- NEM dispatch -------------------------------------------------------------

def test_nem_dispatch_parse_and_aggregate():
    zip_bytes = (FIXTURE_DIR / "dispatch_is.zip").read_bytes()
    df = nem._parse_zip(zip_bytes)
    assert {"SETTLEMENTDATE", "REGIONID", "RRP", "INTERVENTION"}.issubset(df.columns)
    result = nem._aggregate(df)
    by_region = {row["region"]: row for row in result["prices"]}
    assert set(by_region) == {"QLD1", "NSW1", "VIC1", "SA1", "TAS1"}
    for region, row in by_region.items():
        assert isinstance(row["rrp"], float)
        # RRP can occasionally go negative or to the cap; very wide bracket.
        assert -2000 < row["rrp"] < 20_000


# --- DUID registry ------------------------------------------------------------

def test_duid_registry_normalise():
    df = pd.read_excel(
        FIXTURE_DIR / "nem_registration_list.xlsx",
        sheet_name=duid_registry.SHEET,
        engine="openpyxl",
    )
    out = duid_registry.normalise(df)
    assert {"DUID", "Station Name", "Region", "Classification", "fuel_category", "reg_cap_mw"}.issubset(out.columns)
    assert (out["reg_cap_mw"] > 0).all()
    fuels = set(out["fuel_category"].unique())
    # Spot-check the major categories show up.
    assert {"Black coal", "Brown coal", "Gas", "Wind", "Utility solar", "Hydro", "Battery"} <= fuels
    # A known DUID with a stable classification.
    bw01 = out[out["DUID"] == "BW01"]
    assert not bw01.empty
    assert bw01["fuel_category"].iloc[0] == "Black coal"


# --- Next_Day_Dispatch --------------------------------------------------------

def test_dispatch_unit_parse_and_aggregate():
    zip_bytes = (FIXTURE_DIR / "next_day_dispatch.zip").read_bytes()
    df = dispatch_unit._parse_zip(zip_bytes)
    assert {"SETTLEMENTDATE", "DUID", "INTERVENTION", "TRADETYPE", "TOTALCLEARED"}.issubset(df.columns)
    result = dispatch_unit._aggregate(df, gas_day_str="20260503")
    per_duid = result["per_duid"]
    assert {"DUID", "gen_mwh", "load_mwh"}.issubset(per_duid.columns)
    # Expect coverage of hundreds of DUIDs.
    assert len(per_duid) > 200
    # Bayswater unit BW01 should generate a meaningful chunk on most days (660 MW × 24h ≈ 15.8 GWh max).
    bw01 = per_duid[per_duid["DUID"] == "BW01"]
    if not bw01.empty:
        assert 0 <= bw01["gen_mwh"].iloc[0] < 20_000


# --- Rooftop PV ---------------------------------------------------------------

def test_rooftop_pv_parse_one_snapshot():
    """Parse a single half-hourly snapshot. Full-day aggregation is integration-tested elsewhere."""
    zip_bytes = (FIXTURE_DIR / "rooftop_pv.zip").read_bytes()
    df = rooftop_pv._parse_zip(zip_bytes)
    assert {"INTERVAL_DATETIME", "REGIONID", "POWER"}.issubset(df.columns)
    assert set(df["REGIONID"].unique()) == {"NSW1", "QLD1", "SA1", "TAS1", "VIC1"}


# --- Generation mix aggregator ------------------------------------------------

def test_genmix_aggregate_with_synthetic_inputs():
    """End-to-end aggregate() with hand-built mini inputs to verify CF math."""
    registry = pd.DataFrame([
        {"DUID": "GAS1",  "Station Name": "Gas 1",  "Region": "NSW1", "Classification": "Scheduled",      "fuel_category": "Gas",           "reg_cap_mw": 100.0},
        {"DUID": "WIND1", "Station Name": "Wind 1", "Region": "VIC1", "Classification": "Semi-Scheduled", "fuel_category": "Wind",          "reg_cap_mw": 200.0},
        {"DUID": "BATT1", "Station Name": "Batt 1", "Region": "SA1",  "Classification": "Scheduled",      "fuel_category": "Battery",       "reg_cap_mw":  50.0},
    ])
    per_duid = pd.DataFrame([
        {"DUID": "GAS1",  "gen_mwh":  600.0, "load_mwh":   0.0},   # 100 MW × 6h equivalent
        {"DUID": "WIND1", "gen_mwh": 1920.0, "load_mwh":   0.0},   # 200 MW × 9.6h equivalent
        {"DUID": "BATT1", "gen_mwh":  120.0, "load_mwh": 100.0},   # battery cycling
    ])
    rooftop = {"gas_day": "2026-05-03", "total_mwh": 50_000.0, "intervals_used": 48}

    result = genmix.aggregate(registry, per_duid, rooftop, gas_day_str="20260503")
    rows = {r["fuel"]: r for r in result["rows"]}

    # Gas: 600 MWh / (100 MW × 24h) = 25%
    assert rows["Gas"]["cf"] == 25.0
    # Wind: 1920 / (200 × 24) = 40%
    assert rows["Wind"]["cf"] == 40.0
    # Battery: 120 / (50 × 24) = 10%, throughput = 220
    assert rows["Battery"]["cf"] == 10.0
    assert rows["Battery"]["throughput_mwh"] == 220
    # Rooftop: 50000 / (24000 × 24) ≈ 8.7%
    rooftop_row = rows["Rooftop solar (estimated)"]
    assert rooftop_row["mwh"] == 50_000
    assert 8.0 < rooftop_row["cf"] < 9.5


# --- WA GBB -------------------------------------------------------------------

def test_wagbb_aggregate():
    import json
    detail = json.loads((FIXTURE_DIR / "wagbb_actualflow_day.json").read_text(encoding="utf-8"))
    result = wagbb.aggregate(detail)
    assert result["as_of_gas_day"] == "2026-05-02"
    assert result["dbp_flow"] > 0
    assert result["dbp_nameplate"] == 1066  # post-Phase-5-expansion capacity
    assert 0 <= result["dbp_utilisation"] <= 130  # winter peaks may exceed 100% briefly
    facilities = {f["name"] for f in result["facilities"]}
    # Expect at least the major WA producers
    assert "Karratha Gas Plant" in facilities
    assert "Wheatstone" in facilities or "Wheatstone Ashburton West Pipeline" in facilities
