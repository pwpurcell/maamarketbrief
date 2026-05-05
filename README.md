# markets-brief

Self-hosted dashboard for Australian east/west coast gas markets and the NEM electricity market. Auto-refreshes every 15 minutes via APScheduler; optional daily email at 06:30 Australia/Melbourne via Resend.

See `../BRIEF.md` for the full build brief.

## Local development

Requires Python 3.11+.

```bash
# from the markets-brief/ directory
python -m venv .venv

# activate the venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Windows (Git Bash):
source .venv/Scripts/activate
# macOS / Linux:
source .venv/bin/activate

pip install -e .
python -m app.main
```

Then open <http://localhost:8000>. The first refresh tick takes ~20 seconds (downloads Next_Day_Dispatch + 48 rooftop PV files); subsequent page loads are instant from the SQLite cache.

## Diagnostic endpoints

- `/healthz` — liveness probe
- `/cache/status` — JSON dump of latest gas_day + fetched_at per cached source

## Daily email (Resend)

Once the dashboard is running and the cache is warm, the email sender renders the same data into an inline-styled HTML email and ships it via Resend's REST API.

### Setup

1. **Sign up at <https://resend.com>** (free tier covers 3,000 emails/month, plenty for one daily brief).
2. **Verify a sending domain** — Resend will give you SPF + DKIM DNS records to add at your registrar (Cloudflare/Porkbun/etc.). Wait for the dashboard to show the domain as "Verified" (usually a few minutes).
3. **Create an API key** in the Resend dashboard. Copy the `re_…` value.
4. **Add the four env vars** to a `.env` file in the project root (or set them in your shell):

   ```bash
   RESEND_API_KEY=re_your_key_here
   EMAIL_FROM=brief@yourdomain.com   # must be a verified sender address
   EMAIL_TO=paul@example.com         # where the email lands
   ENABLE_DAILY_EMAIL=true           # opt-in flag
   ```

### Test the rendered HTML before sending anything

```bash
# Print the rendered email HTML + subject to stdout (no send)
python -m app.email_sender --dry-run

# Save the rendered HTML to a file, then open it in a browser to check the layout
python -m app.email_sender --to-file preview.html
```

### Send a one-off (manual trigger)

```bash
# Reads env vars; bypasses the ENABLE_DAILY_EMAIL check
python -m app.email_sender --force
```

If `ENABLE_DAILY_EMAIL` is not set to `true`, plain `python -m app.email_sender` exits without sending — that's the safety net for the production timer.

### Production schedule (Phase 7)

A systemd timer fires `python -m app.email_sender` once a day at 06:30 Australia/Melbourne. The unit files land in `deploy/` in Phase 7; for now the sender is just a CLI you can wire to whatever scheduler you prefer (cron, systemd, Windows Task Scheduler, GitHub Actions on a cron schedule, etc.).

## Build phases

1. ✅ Skeleton + hardcoded fake response
2. ✅ East coast gas pipeline flows (GBB)
3. ✅ Remaining gas parsers (STTM, DWGM, GSH) + NEM electricity prices
4. ✅ Generation mix (DUID registry + capacity factors)
5. ✅ WA section, SQLite cache, APScheduler refresh, inline-SVG sparklines
6. ✅ Email mode (Resend)
7. Deploy to VPS (DigitalOcean Sydney)

## What to do when AEMO changes a URL

Endpoints live in `app/sources/*.py`, one module per data source. Each module has a `BASE_URL` constant near the top — edit there. The directory-listing pattern (`<a href="...zip">`) is reused across most sources, so a URL move usually means changing one constant.

## Deploy (Phase 7)

The dashboard runs on a $6/month DigitalOcean droplet behind Caddy with auto-HTTPS. One-shot `deploy/deploy.sh` brings up everything in 5 minutes once the prerequisites are in place.

### One-time setup (do these in order)

#### 1. Push the code to GitHub

If you haven't already, init this directory as a git repo and push to a new GitHub repo (private is fine):

```bash
cd markets-brief
git init
git add .
git commit -m "Initial commit"

# Create the repo on github.com first, then:
git remote add origin https://github.com/YOUR_USERNAME/markets-brief.git
git branch -M main
git push -u origin main
```

The `.env` file is already in `.gitignore`, so secrets won't leak. Logo files in `app/static/` are committed (they're not secret).

#### 2. Register a domain

[Cloudflare Registrar](https://www.cloudflare.com/products/registrar/) is at-cost (no markup) with free DNS. Pick a domain — e.g. `mountainashadvisory.com.au` — then plan to point a subdomain like `brief.mountainashadvisory.com.au` at the VPS.

If you already own a domain, skip this and just add a subdomain A record in step 4.

#### 3. Create the DigitalOcean droplet

1. <https://cloud.digitalocean.com/droplets/new>
2. Choose **Sydney** region (lowest latency to AEMO endpoints)
3. **Ubuntu 24.04 LTS x64**
4. **Basic / Premium AMD / $6/mo** (1 vCPU, 1GB RAM, 25GB SSD — plenty for this workload)
5. Add your SSH key (or pick a root password)
6. Hostname: `markets-brief` (or whatever)
7. Create. Wait ~30 seconds, copy the public IPv4 address.

#### 4. Point DNS at the droplet

In your registrar's DNS panel (Cloudflare or whoever):

| Type | Name | Value | TTL |
|---|---|---|---|
| A | `brief` (or `@` for root) | `<droplet IP>` | Auto / 300 |

Wait 1–5 minutes for DNS to propagate. Verify with `dig brief.yourdomain.com +short` — it should return your droplet IP.

#### 5. Run the deploy script

SSH into the droplet:

```bash
ssh root@<droplet IP>
```

Then run the one-shot. **Replace** `YOUR_USERNAME` with your GitHub username and `brief.yourdomain.com` with your DNS name:

```bash
curl -fsSL https://raw.githubusercontent.com/YOUR_USERNAME/markets-brief/main/deploy/deploy.sh \
  | sudo bash -s -- \
    REPO_URL=https://github.com/YOUR_USERNAME/markets-brief.git \
    DOMAIN=brief.yourdomain.com
```

The script:
- Installs Python 3.12, git, and Caddy
- Creates a `markets-brief` system user
- Clones the repo to `/opt/markets-brief`
- Sets up the venv, installs deps
- Prompts you for Resend API key + email addresses (skip with Enter if you don't have Resend yet)
- Installs and starts the systemd app service + the daily-email timer
- Configures Caddy with auto-HTTPS via Let's Encrypt

When it finishes, **<https://brief.yourdomain.com>** is live. The first refresh tick takes ~30 seconds to populate the cache from cold (it downloads Next_Day_Dispatch and 48 rooftop PV files).

### Daily operations

```bash
# Tail the dashboard logs
sudo journalctl -u markets-brief -f

# Check the email timer's next-fire time
sudo systemctl list-timers markets-email.timer

# Tail email send logs (one entry per day)
sudo journalctl -u markets-email -f

# Force a manual email send (ignores ENABLE_DAILY_EMAIL=false)
sudo -u markets-brief /opt/markets-brief/.venv/bin/python -m app.email_sender --force
```

### Updating after code changes

Push to GitHub from your local repo, then on the droplet:

```bash
sudo bash /opt/markets-brief/deploy/update.sh
```

That re-pulls `origin/main`, reinstalls deps, reinstalls systemd units (in case they changed), restarts the service, and reloads Caddy.

### Editing secrets after deploy

```bash
sudo nano /opt/markets-brief/.env
sudo systemctl restart markets-brief
```

### Resend setup (optional — for the daily 06:30 email)

If you skipped Resend during deploy and want to enable it now:

1. Sign up at <https://resend.com> (free tier covers 3,000 emails/month).
2. Add a sending domain — Resend gives you SPF + DKIM DNS records to paste into your registrar.
3. Wait a few minutes for verification.
4. Create an API key in the Resend dashboard.
5. Edit `/opt/markets-brief/.env` on the droplet and set:
   ```
   RESEND_API_KEY=re_your_key
   EMAIL_FROM=brief@yourdomain.com   # must match the verified sender
   EMAIL_TO=you@example.com
   ENABLE_DAILY_EMAIL=true
   ```
6. Test with `sudo -u markets-brief /opt/markets-brief/.venv/bin/python -m app.email_sender --force` — should land in your inbox in seconds.
7. The daily timer fires automatically at 06:30 Australia/Melbourne.

### What if it breaks

- **Dashboard returns 502 / connection refused**: app crashed. `sudo journalctl -u markets-brief -n 100` shows the error. Common cause: a fetcher hit an AEMO 5xx — should self-heal on the next 15-min refresh. Otherwise `sudo systemctl restart markets-brief`.
- **Caddy says "no certificate"**: DNS hasn't propagated yet. Wait 5 minutes, run `sudo systemctl restart caddy`.
- **Cache empty after a fresh deploy**: first refresh tick is async on startup; takes ~30 s. Reload after a minute. `/cache/status` shows what's landed.
- **Email not arriving**: check `sudo journalctl -u markets-email -n 50`. Common causes: Resend domain not verified, EMAIL_FROM doesn't match a verified sender, `ENABLE_DAILY_EMAIL` not set to `true`.
- **Want to nuke the cache and start fresh**: `sudo rm /opt/markets-brief/app/data/cache.db && sudo systemctl restart markets-brief`
