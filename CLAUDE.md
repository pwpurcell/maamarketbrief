# CLAUDE.md — state-of-the-project for any future Claude Code session

This file is the canonical "where things stand" document. Read it first when picking up the markets-brief project.

## What this is

**The Mountain Ash Advisory Energy Brief** — a self-hosted dashboard + daily email for the Australian east-coast and WA gas markets, NEM electricity dispatch, generation mix, storage, forward curves, LNG netback, and macro context.

- **Live**: <https://maaenergybrief.com.au>
- **Repo**: <https://github.com/pwpurcell/maamarketbrief> (public)
- **Owner**: Paul Purcell, Mountain Ash Advisory (energy policy + government relations consultancy in Australia). **Not a developer** — explain changes in plain English, prioritise reliability + readability over cleverness.

## Deployment

- **VPS**: DigitalOcean Sydney, $6/mo, Ubuntu 24.04, IP `134.199.168.240`.
- **Service user**: `markets-brief` (system user, no shell).
- **Install path**: `/opt/markets-brief`.
- **Reverse proxy**: Caddy on `0.0.0.0:80/443` → uvicorn on `127.0.0.1:8000`. Auto-HTTPS via Let's Encrypt.
- **systemd units**: `markets-brief.service` (FastAPI), `markets-email.timer` + `markets-email.service` (06:30 Australia/Melbourne daily).
- **SSH**: `ssh root@134.199.168.240` from Paul's Windows machine via PowerShell.

## Architecture (one-paragraph version)

APScheduler runs in the FastAPI app process and refreshes a SQLite cache (`app/data/cache.db`) every 15 minutes by calling each source module's `fetch()`. The dashboard route reads from cache only — no live AEMO calls per request, so page load is ~100ms. The email sender (`python -m app.email_sender`) builds the same snapshot via `app/snapshot.py` and ships via Resend. Snapshot composition is shared between dashboard and email.

## File map

```
markets-brief/
├── app/
│   ├── main.py                  # FastAPI app, routes, startup hook
│   ├── snapshot.py              # build_snapshot() — cache reads, used by both dashboard + email
│   ├── cache.py                 # SQLite DAL (init / put / get_latest / get_history / status)
│   ├── scheduler.py             # APScheduler config, FAST_JOBS list of all fetchers
│   ├── email_sender.py          # CLI: python -m app.email_sender [--dry-run|--to-file PATH|--force]
│   ├── sparkline.py             # inline SVG sparkline generator
│   ├── config.py                # pipeline metadata, fuel categories, AEMO_HEADERS, REFRESH_INTERVAL_MINUTES
│   ├── sources/
│   │   ├── _nemweb.py           # shared parser for AEMO I/D/C row CSVs
│   │   ├── gbb.py               # GBB nominations (east coast pipeline flows)
│   │   ├── sttm.py              # STTM ex-ante prices
│   │   ├── dwgm.py              # DWGM 6am price
│   │   ├── gsh.py               # Wallumbilla benchmark
│   │   ├── nem.py               # NEM 5-min dispatch RRP
│   │   ├── interconnectors.py   # NEM interconnector flows (same DispatchIS file)
│   │   ├── dispatch_unit.py     # Next_Day_Dispatch UNIT_SOLUTION (per-DUID MWh)
│   │   ├── duid_registry.py     # AEMO Registration List XLS, parquet cache
│   │   ├── rooftop_pv.py        # ROOFTOP_PV/ACTUAL (48 half-hourly files per gas day)
│   │   ├── genmix.py            # combines registry + dispatch + rooftop into per-fuel CFs
│   │   ├── storage.py           # GBB ActualFlowStorage STOR rows + WORKING_CAPACITY_PJ + MAX_WITHDRAWAL_TJD + CUSHION_GAS_PJ
│   │   ├── demand_forecast.py   # DWGM int153 + STTM int652
│   │   ├── wagbb.py             # WA GBB modern API
│   │   ├── asx_futures.py       # asxenergy.com.au scrape (gas + elec forwards)
│   │   ├── accc.py              # ACCC monthly LNG netback Excel
│   │   ├── macro.py             # RBA F11.1 + F1.1, FRED DGS10/Brent/HH
│   │   └── equities.py          # asx.api.markitdigital.com (Markit Digital ASX backend)
│   ├── templates/
│   │   ├── dashboard.html       # tabbed: Gas / Electricity / Forwards / Macro
│   │   └── email.html           # single-scroll, Outlook-safe inline styles
│   ├── static/
│   │   ├── style.css
│   │   ├── maa-logo.png         # 1024x1024 full-res for browser
│   │   └── maa-logo-email.png   # 400x400 ~70KB for inline base64 in email
│   └── data/                    # gitignored; SQLite cache + DUID registry parquet live here
├── deploy/
│   ├── deploy.sh                # one-shot bootstrap (idempotent)
│   ├── update.sh                # git pull + restart (NB: pip step has known bug)
│   ├── markets-brief.service    # systemd unit for the FastAPI app
│   ├── markets-email.service    # systemd oneshot for the daily email
│   ├── markets-email.timer      # 06:30 Australia/Melbourne
│   └── Caddyfile                # reverse proxy + auto-HTTPS
├── tests/
│   ├── test_parsers.py          # 14 tests against committed fixtures
│   └── fixtures/                # real AEMO files committed for reproducible tests
├── BRIEF.md                     # original brief (historical reference)
├── README.md                    # user-facing setup + ops docs
├── CLAUDE.md                    # this file
├── pyproject.toml
├── .env.example
├── .gitattributes               # LF for sh/service/timer/Caddyfile (CRITICAL on Windows)
└── .gitignore
```

## Live data sources

All sources currently wired. See README and `app/sources/*.py` for endpoint URLs and per-source quirks.

| Section | Source | Status |
|---|---|---|
| East coast pipelines | AEMO GBB nominations (today's gas day) | Live |
| Gas prices | AEMO STTM/DWGM/GSH | Live |
| NEM dispatch RRP | AEMO DispatchIS 5-min | Live |
| Interconnectors | AEMO DispatchIS INTERCONNECTORRES | Live |
| Generation mix | AEMO Next_Day_Dispatch + ROOFTOP_PV + DUID registry | Live (yesterday's gas day) |
| WA gas | gbbwa.aemo.com.au /api/v1 | Live (~2-day lag, settled actuals) |
| Gas storage | AEMO GBB ActualFlowStorage | Live (~1-2 day lag) |
| Demand forecast | AEMO DWGM int153 + STTM int652 | Live (forecast only) |
| Forward curves | asxenergy.com.au public dataset | Live (~20 min delayed) |
| LNG netback (Brent + Henry Hub) | FRED CSVs | Live (~3-7 day FRED lag) |
| LNG netback (ACCC) | ACCC monthly Excel | Live |
| Macro FX/rates | RBA F11.1 + F1.1, FRED DGS10 | Live |
| Energy equities | asx.api.markitdigital.com (ASX backend) | Live, 15 ASX-listed names |

## Still placeholder / awaiting

- JKM, TTF — paid (Platts, ICE). Paul to organise.
- Internal netback row — needs JKM.
- Demand actuals comparison — code path ready, fetcher not built.
- WA indicative domgas reference price — no clean public source.

## Critical gotchas

1. **Caddyfile `{$DOMAIN}` env-var doesn't substitute** on Ubuntu 24.04 modern Caddy. Live droplet was patched manually with `sudo sed -i 's|{$DOMAIN}|maaenergybrief.com.au|g' /etc/caddy/Caddyfile`. **TODO**: patch deploy.sh to substitute at deploy time instead of using `{$DOMAIN}`.

2. **deploy/update.sh pip step fails** on Python 3.12 editable installs. For template-only changes use `sudo systemctl restart markets-brief` instead. **TODO**: make update.sh skip pip when pyproject.toml hasn't changed.

3. **`.gitattributes` enforces LF** for shell scripts, systemd units, and Caddyfile — critical because Windows CRLF breaks them on Linux. Don't disable.

4. **Scraped sources are TOS-grey** for paid redistribution: asxenergy.com.au + Markit Digital ASX backend are personal-use only. Drop or licence before monetising.

5. **Iona storage capacity discrepancy**: AEMO file says 24.4 PJ effective March 2024; Paul has it set to 28 PJ per Lochard's stated post-2024 expansion. The 28 figure is current-operator-disclosed, the 24.4 is current-AEMO. Comment in `app/sources/storage.py` notes this.

## Workflow for changes

```bash
# Local
cd "/c/Users/paul/OneDrive/Documents/Claude Code/markets-brief"
# ... edit files ...
git add -A && git commit -m "..." && git push

# Then on droplet
ssh root@134.199.168.240
sudo bash /opt/markets-brief/deploy/update.sh
# Or for template/CSS-only changes (faster, sidesteps the pip bug):
sudo systemctl restart markets-brief
sudo systemctl reload caddy
```

## Useful commands on the droplet

```bash
# Tail dashboard logs
sudo journalctl -u markets-brief -f

# Cache state (which sources have landed)
curl https://maaenergybrief.com.au/cache/status | jq

# Force a manual email send (currently disabled in env)
sudo -u markets-brief /opt/markets-brief/.venv/bin/python -m app.email_sender --force

# Edit production secrets (e.g. enable email)
sudo nano /opt/markets-brief/.env
sudo systemctl restart markets-brief

# Wipe cache and restart (rare; only if cache corruption suspected)
sudo systemctl stop markets-brief
sudo rm /opt/markets-brief/app/data/cache.db
sudo systemctl start markets-brief
```

## Open product decisions Paul is mulling

- **Monetisation**: leaning toward $10/mo SaaS for solo operators ("AEMO done right, daily"), but undecided. Lead-gen-for-consulting is the highest-margin path; $10/mo is the "make a side income" path; enterprise is the "needs warm relationships" path.
- **Domain**: keep `maaenergybrief.com.au` or move to `mountainashadvisory.com.au/brief` for unified consultancy branding.
- **Make-it-really-good features**: anomaly highlighting (auto-flag binding interconnectors, price spikes etc.), historical comparison toggles, custom alerts, mobile pass, daily commentary slot. These are the next round of high-value-add work.

## Conversation history breadcrumb

Built across one extended conversation 2026-05-04 to 2026-05-05. Phases 1-7 of the original BRIEF.md all complete + significant Phase 3 UI extensions (storage and balance, forward curves, LNG netback, macro context) live; tabbed UI added on 2026-05-05; deployed to DO Sydney with Mountain Ash Advisory branding. See git log for the chronological build trail.
