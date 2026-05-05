"""Build the dashboard snapshot dict from the SQLite cache.

Used by both the FastAPI dashboard route (web view) and the email sender
(daily HTML email). Pure cache reads — no live AEMO calls happen here.
The scheduler is the only thing that hits AEMO; both consumers share the
same cached data."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from app import cache
from app.config import FUEL_CATEGORIES, NEM_REGIONS, PIPELINES, REFRESH_INTERVAL_MINUTES


log = logging.getLogger(__name__)

# Australia/Melbourne is UTC+10 in winter, UTC+11 in DST. Phase 5 still uses a
# fixed offset for the header timestamp; correct DST handling can come later.
MELB_OFFSET = timezone(timedelta(hours=10), name="AEST")


# --- Helpers ------------------------------------------------------------------

def _mean(values: list) -> float | None:
    nums = [v for v in values if v is not None]
    return round(sum(nums) / len(nums), 2) if nums else None


def _series_from_history(history: list[dict], list_key: str, label_field: str, label_value: str, value_field: str) -> list:
    """Extract a single hub/region's values across N cache snapshots."""
    out: list = []
    for entry in history:
        for row in entry["payload"].get(list_key, []):
            if row.get(label_field) == label_value:
                out.append(row.get(value_field))
                break
    return out


# --- Anomaly thresholds + tagging ---------------------------------------------

# Cell- and row-level severity thresholds. Tuned to surface notable conditions
# without colouring everything. Adjust in one place to retune the dashboard.
PIPELINE_UTIL_WARN_PCT = 90
PIPELINE_UTIL_CRIT_PCT = 98
INTERCONNECTOR_UTIL_CRIT_PCT = 95   # any interconnector above this is binding
RRP_WARN_AUD = 150
RRP_CRIT_AUD = 300
RRP_NEGATIVE_AUD = 0                # negative pricing = renewable curtailment / oversupply
STORAGE_PCT_WARN = 30
STORAGE_PCT_CRIT = 15
STORAGE_DAYS_COVER_WARN = 14
STORAGE_DAYS_COVER_CRIT = 7


def _severity_from_util(util: float | None, warn: float, crit: float) -> str:
    if util is None:
        return "normal"
    if util >= crit:
        return "critical"
    if util >= warn:
        return "warning"
    return "normal"


def _tag_pipeline_severity(rows: list[dict]) -> None:
    for r in rows:
        r["severity"] = _severity_from_util(r.get("utilisation"), PIPELINE_UTIL_WARN_PCT, PIPELINE_UTIL_CRIT_PCT)


def _tag_elec_severity(rows: list[dict]) -> None:
    for r in rows:
        rrp = r.get("rrp")
        if rrp is None:
            r["severity"] = "normal"
        elif rrp >= RRP_CRIT_AUD:
            r["severity"] = "critical"
        elif rrp >= RRP_WARN_AUD or rrp < RRP_NEGATIVE_AUD:
            # Negative RRP shares the warning tone (notable, not necessarily bad).
            r["severity"] = "warning"
        else:
            r["severity"] = "normal"


def _tag_interconnector_severity(rows: list[dict]) -> None:
    for r in rows:
        r["severity"] = _severity_from_util(r.get("utilisation"), INTERCONNECTOR_UTIL_CRIT_PCT, INTERCONNECTOR_UTIL_CRIT_PCT)


def _tag_storage_severity(rows: list[dict]) -> None:
    for r in rows:
        pct = r.get("pct_full")
        days = r.get("days_cover")
        sev = "normal"
        if pct is not None and pct < STORAGE_PCT_CRIT:
            sev = "critical"
        elif days is not None and days < STORAGE_DAYS_COVER_CRIT:
            sev = "critical"
        elif pct is not None and pct < STORAGE_PCT_WARN:
            sev = "warning"
        elif days is not None and days < STORAGE_DAYS_COVER_WARN:
            sev = "warning"
        r["severity"] = sev


def _compute_notable(pipelines: list[dict], elec_prices: list[dict], interconnectors_rows: list[dict], storage_rows: list[dict]) -> list[dict]:
    """Build the "Today's notable" bullet list from already-tagged rows.

    Returns 0–6 items, sorted critical-first."""
    items: list[dict] = []

    for p in pipelines:
        if p.get("severity") in ("critical", "warning") and p.get("utilisation") is not None:
            label = "binding" if p["severity"] == "critical" else "high utilisation"
            items.append({
                "text": f"{p['display']} — {p['utilisation']:.0f}% {label}",
                "severity": p["severity"],
                "tab": "tab-gas",
            })

    for e in elec_prices:
        if e.get("rrp") is None:
            continue
        if e.get("severity") == "critical":
            items.append({
                "text": f"{e['region']} dispatch RRP ${e['rrp']:.0f}/MWh — high",
                "severity": "critical",
                "tab": "tab-electricity",
            })
        elif e.get("rrp") < RRP_NEGATIVE_AUD:
            items.append({
                "text": f"{e['region']} dispatch negative — ${e['rrp']:.2f}/MWh (renewables curtailment / oversupply)",
                "severity": "warning",
                "tab": "tab-electricity",
            })
        elif e.get("severity") == "warning":
            items.append({
                "text": f"{e['region']} dispatch RRP ${e['rrp']:.0f}/MWh — elevated",
                "severity": "warning",
                "tab": "tab-electricity",
            })

    for r in interconnectors_rows:
        if r.get("severity") == "critical":
            items.append({
                "text": f"{r['display']} binding: {r['direction_text']} at {r['utilisation']:.0f}%",
                "severity": "critical",
                "tab": "tab-electricity",
            })

    for s in storage_rows:
        if s.get("severity") == "critical":
            label = "low" if s.get("pct_full") is not None and s["pct_full"] < STORAGE_PCT_CRIT else "stress-day cover"
            pct = s.get("pct_full")
            items.append({
                "text": f"{s['facility']}: {pct:.0f}% full — {label}" if pct is not None else f"{s['facility']}: stress-day cover",
                "severity": "critical",
                "tab": "tab-gas",
            })
        elif s.get("severity") == "warning":
            pct = s.get("pct_full")
            if pct is not None:
                items.append({
                    "text": f"{s['facility']}: {pct:.0f}% full",
                    "severity": "warning",
                    "tab": "tab-gas",
                })

    # Sort: critical first, then warnings; cap at 6 items so the box stays scannable.
    return sorted(items, key=lambda x: 0 if x["severity"] == "critical" else 1)[:6]


# --- Per-source cache reads ---------------------------------------------------

def _empty_pipelines() -> list[dict]:
    """Layout-preserving rows when the GBB cache is empty (server warming up)."""
    return [
        {
            "id": spec.id,
            "display": spec.display,
            "direction": spec.direction,
            "flow": None,
            "nameplate": spec.nameplate_tjd,
            "utilisation": None,
            "delta_arrow": "",
            "delta_class": "flat",
            "delta_value": None,
            "stale": True,
        }
        for spec in PIPELINES
    ]


def _read_pipelines() -> tuple[list[dict], str | None]:
    entry = cache.get_latest("gbb")
    if not entry:
        rows = _empty_pipelines()
        _tag_pipeline_severity(rows)
        return rows, None
    payload = entry["payload"]
    rows = payload.get("pipelines", _empty_pipelines())
    _tag_pipeline_severity(rows)
    return rows, payload.get("as_of_gas_day")


def _read_gas_prices() -> list[dict]:
    """STTM + DWGM + GSH merged. Sparkline + 7d avg from cache history per hub."""
    rows: list[dict] = []
    fallback_hubs = {
        "sttm": ["STTM Sydney", "STTM Adelaide", "STTM Brisbane"],
        "dwgm": ["DWGM (6am)"],
        "gsh":  ["Wallumbilla GSH"],
    }
    for source, hubs in fallback_hubs.items():
        entry = cache.get_latest(source)
        history = cache.get_history(source, n=7)
        if entry:
            for hub_row in entry["payload"].get("prices", []):
                hub_label = hub_row.get("hub")
                values = _series_from_history(history, "prices", "hub", hub_label, "today")
                rows.append({**hub_row, "avg7": _mean(values), "sparkline_values": values})
        else:
            for hub in hubs:
                rows.append({"hub": hub, "today": None, "avg7": None, "stale": True, "sparkline_values": []})
    return rows


def _read_elec_prices() -> list[dict]:
    """Latest dispatch RRP + 7d avg + sparkline per region."""
    entry = cache.get_latest("nem")
    history = cache.get_history("nem", n=7)
    rows: list[dict] = []
    for region in NEM_REGIONS:
        values = _series_from_history(history, "prices", "region", region, "rrp")
        avg7 = _mean(values)
        if entry:
            current = next(
                (r for r in entry["payload"].get("prices", []) if r.get("region") == region),
                None,
            )
            if current is not None:
                rows.append({**current, "avg7": avg7, "sparkline_values": values})
                continue
        rows.append({"region": region, "rrp": None, "avg7": avg7, "stale": True, "sparkline_values": values})
    _tag_elec_severity(rows)
    return rows


def _read_interconnectors() -> dict:
    entry = cache.get_latest("interconnectors")
    if not entry:
        return {"rows": [], "as_of": None}
    payload = entry["payload"]
    _tag_interconnector_severity(payload.get("rows", []))
    return payload


def _read_weather() -> dict:
    entry = cache.get_latest("weather")
    if not entry:
        return {"rows": []}
    return entry["payload"]


def _read_genmix() -> tuple[list[dict], str | None]:
    entry = cache.get_latest("genmix")
    if not entry:
        empty = [
            {"fuel": cat, "cf": None, "mwh": 0, "installed_mw": 0, "avg7_cf": None, "delta": None, "share_pct": 0.0, "stale": True}
            for cat in FUEL_CATEGORIES
        ]
        return empty, None
    payload = entry["payload"]
    return payload.get("rows", []), payload.get("gas_day")


def _read_wa() -> dict:
    entry = cache.get_latest("wagbb")
    if not entry:
        return {
            "dbp_flow": None,
            "dbp_nameplate": 845,
            "dbp_utilisation": None,
            "facilities": [],
            "indicative_price": None,
            "as_of_gas_day": None,
            "stale": True,
        }
    payload = entry["payload"]
    return {**payload, "stale": False}


def _empty_storage_rows() -> list[dict]:
    return [
        {"facility": name, "inventory_pj": None, "capacity_pj": None, "pct_full": None,
         "net_flow_tjd": None, "net_flow_arrow": "", "net_flow_class": "flat",
         "days_cover": None, "region": "", "stale": True}
        for name in ("Iona UGS", "Dandenong LNG", "Newcastle Gas Storage", "Roma Underground (Origin)", "Silver Springs (QGC)", "Moomba (Santos)")
    ]


def _empty_demand_cards() -> list[dict]:
    return [
        {"market": m, "forecast": None, "actual": None, "unit": "TJ", "diff": None, "pct": None, "tone_class": "flat", "context": "warming up", "stale": True}
        for m in ("DWGM (Victoria)", "STTM (SYD+BNE+ADL)")
    ]


def _read_storage() -> dict:
    """Storage and balance section: cached storage + demand_forecast fetches."""
    storage_entry = cache.get_latest("storage")
    demand_entry = cache.get_latest("demand")
    gas_storage = storage_entry["payload"]["rows"] if storage_entry else _empty_storage_rows()
    demand_cards = demand_entry["payload"]["cards"] if demand_entry else _empty_demand_cards()
    _tag_storage_severity(gas_storage)
    return {"gas_storage": gas_storage, "demand": demand_cards}


def _read_forwards() -> dict:
    """ASX 24 settlement prices via the asxenergy.com.au public dataset."""
    entry = cache.get_latest("asx_futures")
    if not entry:
        return {"gas_sections": [], "elec_sections": [], "as_of_time": None}
    payload = entry["payload"]
    elec_sections = [
        s for s in payload.get("elec_sections", [])
        if "Base Quarter" in s["section"] or "Base Strip" in s["section"]
    ]
    return {
        "gas_sections": payload.get("gas_sections", []),
        "elec_sections": elec_sections,
        "as_of_time": payload.get("as_of_time"),
    }


# Equity tickers — must stay in sync with app.sources.equities.TICKERS.
_EQUITY_TICKERS = [
    ("AEL", "Amplitude Energy",          "Upstream gas (formerly Cooper Energy)"),
    ("AGL", "AGL Energy",                "Electricity / gas retail + generation"),
    ("ALD", "Ampol",                     "Fuels (refining + retail)"),
    ("APA", "APA Group",                 "Gas pipelines + electricity transmission"),
    ("BPT", "Beach Energy",              "Upstream gas / oil"),
    ("KAR", "Karoon Energy",             "Upstream oil"),
    ("NHC", "New Hope Corporation",      "Coal"),
    ("ORG", "Origin Energy",             "Electricity / gas retail + APLNG stake"),
    ("SMR", "Stanmore Resources",        "Coal"),
    ("STO", "Santos",                    "Upstream gas / LNG"),
    ("STX", "Strike Energy",             "Upstream gas"),
    ("VEA", "Viva Energy",               "Fuels (refining + retail)"),
    ("WDS", "Woodside Energy",           "LNG / oil"),
    ("WHC", "Whitehaven Coal",           "Coal"),
    ("YAL", "Yancoal Australia",         "Coal"),
]


def _empty_equities() -> list[dict]:
    return [
        {"ticker": t, "company": c, "segment": s, "last": None, "delta_today_pct": None, "delta_30d_pct": None, "mcap_bn": None, "stale": True}
        for t, c, s in sorted(_EQUITY_TICKERS, key=lambda r: r[0])
    ]


def _empty_fx_rates() -> list[dict]:
    return [
        {"label": label, "value_text": "—", "delta_text": "", "context": "warming up", "tone_class": "flat"}
        for label in ("AUD/USD", "RBA cash rate target", "US 10y Treasury")
    ]


def _read_macro() -> dict:
    macro_entry = cache.get_latest("macro")
    fx_rates = macro_entry["payload"]["fx_rates"] if macro_entry else _empty_fx_rates()
    eq_entry = cache.get_latest("equities")
    equities_rows = eq_entry["payload"]["rows"] if eq_entry else _empty_equities()
    return {"fx_rates": fx_rates, "equities": equities_rows}


def _placeholder_global_card(series: str, unit: str) -> dict:
    return {
        "series": series, "value_main": None, "unit_main": unit,
        "value_alt": None, "unit_alt": None,
        "delta": None, "delta_arrow": "", "delta_class": "flat",
        "context": "Awaiting paid feed", "stale": True,
    }


def _read_netback() -> dict:
    """LNG Netback section: ACCC + macro Brent/HH cards + cross-refs from STTM/GSH cache."""
    macro_entry = cache.get_latest("macro")
    accc_entry = cache.get_latest("accc")
    sttm_entry = cache.get_latest("sttm")
    gsh_entry = cache.get_latest("gsh")

    global_lng: list[dict] = []
    global_lng.append(_placeholder_global_card("JKM front-month", "USD/MMBtu"))
    global_lng.append(_placeholder_global_card("TTF front-month", "USD/MMBtu"))
    if macro_entry:
        global_lng.extend(macro_entry["payload"].get("global_lng", []))
    else:
        global_lng.append(_placeholder_global_card("Brent front-month", "USD/bbl"))
        global_lng.append(_placeholder_global_card("Henry Hub front-month", "USD/MMBtu"))

    netback: list[dict] = []
    if accc_entry:
        accc_payload = accc_entry["payload"]
        netback.append({
            "series": f"ACCC LNG netback (historical, {accc_payload['month_label']})",
            "current": accc_payload["netback_aud_per_gj"],
            "avg7": None, "avg30": None,
            "spread_sttm_syd": None, "spread_wallum": None,
            "stale": False,
        })
    else:
        netback.append({
            "series": "ACCC LNG netback", "current": None,
            "avg7": None, "avg30": None,
            "spread_sttm_syd": None, "spread_wallum": None, "stale": True,
        })

    sttm_prices = sttm_entry["payload"].get("prices", []) if sttm_entry else []
    by_hub = {p["hub"]: p for p in sttm_prices}
    gsh_price = None
    if gsh_entry:
        gsh_prices = gsh_entry["payload"].get("prices", [])
        gsh_price = gsh_prices[0]["today"] if gsh_prices else None

    def _spread(value: float | None, ref: float | None) -> float | None:
        return None if (value is None or ref is None) else round(value - ref, 2)

    sttm_syd = by_hub.get("STTM Sydney", {}).get("today")
    sttm_bri = by_hub.get("STTM Brisbane", {}).get("today")

    netback.append({
        "series": "STTM Sydney",
        "current": sttm_syd, "avg7": None, "avg30": None,
        "spread_sttm_syd": 0.0 if sttm_syd is not None else None,
        "spread_wallum": _spread(sttm_syd, gsh_price),
        "stale": sttm_syd is None,
    })
    netback.append({
        "series": "STTM Brisbane",
        "current": sttm_bri, "avg7": None, "avg30": None,
        "spread_sttm_syd": _spread(sttm_bri, sttm_syd),
        "spread_wallum": _spread(sttm_bri, gsh_price),
        "stale": sttm_bri is None,
    })
    netback.append({
        "series": "Wallumbilla GSH",
        "current": gsh_price, "avg7": None, "avg30": None,
        "spread_sttm_syd": _spread(gsh_price, sttm_syd),
        "spread_wallum": 0.0 if gsh_price is not None else None,
        "stale": gsh_price is None,
    })

    return {
        "global_lng": global_lng,
        "netback": netback,
        "footnote": "ACCC netback is the official monthly historical series. STTM/GSH rows are today's published prices. JKM/TTF cards await a paid feed; \"Internal netback\" calc deferred until JKM lands.",
    }


# --- Composition --------------------------------------------------------------

def build_snapshot() -> dict:
    """Compose every section's data into the dict the templates expect."""
    now = datetime.now(MELB_OFFSET)

    pipelines, pipelines_gas_day = _read_pipelines()
    gas_prices = _read_gas_prices()
    elec_prices = _read_elec_prices()
    genmix_rows, genmix_gas_day = _read_genmix()
    genmix_gas_day_iso = (
        f"{genmix_gas_day[:4]}-{genmix_gas_day[4:6]}-{genmix_gas_day[6:8]}"
        if genmix_gas_day and len(genmix_gas_day) == 8 and genmix_gas_day.isdigit()
        else genmix_gas_day
    )
    wa = _read_wa()
    storage_section = _read_storage()
    interconnectors_section = _read_interconnectors()
    notable = _compute_notable(
        pipelines=pipelines,
        elec_prices=elec_prices,
        interconnectors_rows=interconnectors_section.get("rows", []),
        storage_rows=storage_section.get("gas_storage", []),
    )

    status = cache.status()
    sources_present = sorted(status.keys())
    sources_missing = [s for s in ("gbb", "sttm", "dwgm", "gsh", "nem", "genmix", "wagbb") if s not in status]

    banner_bits: list[str] = []
    if not sources_present:
        banner_bits.append("Cache warming up — first refresh in progress, page will populate once sources land. Refresh in a few seconds.")
    else:
        banner_bits.append("All live sources cache-backed (15-min refresh).")
        if pipelines_gas_day:
            banner_bits.append(f"GBB nominations: {pipelines_gas_day}.")
        if genmix_gas_day:
            gd = f"{genmix_gas_day[:4]}-{genmix_gas_day[4:6]}-{genmix_gas_day[6:8]}" if len(genmix_gas_day) == 8 and genmix_gas_day.isdigit() else genmix_gas_day
            banner_bits.append(f"Generation mix: {gd}.")
        if wa.get("as_of_gas_day"):
            banner_bits.append(f"WA gas: {wa['as_of_gas_day']}.")
        if sources_missing:
            banner_bits.append(f"Still waiting on: {', '.join(sources_missing)}.")

    return {
        "as_of": now,
        "as_of_str": now.strftime("%A, %d %B %Y, %H:%M %Z"),
        "title_date": now.strftime("%A, %d %B %Y"),
        "next_refresh_min": REFRESH_INTERVAL_MINUTES,
        "notable": notable,
        "weather": _read_weather(),
        "pipelines": pipelines,
        "gas_prices": gas_prices,
        "elec_prices": elec_prices,
        "nem_regions": NEM_REGIONS,
        "interconnectors": interconnectors_section,
        "genmix": genmix_rows,
        "genmix_gas_day": genmix_gas_day_iso,
        "wa": wa,
        "storage": storage_section,
        "forwards": _read_forwards(),
        "netback": _read_netback(),
        "macro": _read_macro(),
        "phase_banner": " ".join(banner_bits),
        "phase3_ui_banner": "",
    }
