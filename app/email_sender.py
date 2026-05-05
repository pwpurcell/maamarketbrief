"""Daily-brief email sender — `python -m app.email_sender`.

Reads the same SQLite cache the dashboard reads, renders email.html into an
inline-styled HTML body, and POSTs to Resend's REST API.

Configuration (.env or environment):
    RESEND_API_KEY      Resend API key (re_…)
    EMAIL_FROM          Verified sender, e.g. brief@yourdomain.com
    EMAIL_TO            Recipient address
    ENABLE_DAILY_EMAIL  "true" to enable; anything else exits early without sending

Designed to be triggered by a systemd timer at 06:30 Australia/Melbourne (Phase 7).

CLI flags:
    --dry-run           Render and print HTML (and subject) — no send
    --to-file PATH      Render and save HTML to PATH — useful for visual review in a browser
    --force             Send even if ENABLE_DAILY_EMAIL is unset (for ad-hoc test sends)
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import logging
import os
import sys
from pathlib import Path

import httpx
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.snapshot import build_snapshot


log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
RESEND_API_URL = "https://api.resend.com/emails"


def render_email() -> tuple[str, str]:
    """Build the snapshot, render email.html, return (subject, html_body)."""
    env = Environment(
        loader=FileSystemLoader(str(APP_DIR / "templates")),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("email.html")
    snapshot = build_snapshot()

    # Inline the email-sized logo as a base64 data URI so the email is
    # self-contained (no external image hosting required, no remote-image
    # blocking by Outlook/Gmail).
    logo_path = APP_DIR / "static" / "maa-logo-email.png"
    if logo_path.exists():
        snapshot["logo_b64"] = base64.b64encode(logo_path.read_bytes()).decode()
    else:
        snapshot["logo_b64"] = ""

    html = template.render(**snapshot)
    subject = f"Mountain Ash Advisory Energy Brief — {dt.date.today().strftime('%d %b %Y')}"
    return subject, html


def send_via_resend(subject: str, html: str, *, api_key: str, sender: str, recipient: str) -> dict:
    """POST the rendered email to Resend. Raises on non-2xx."""
    resp = httpx.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": sender,
            "to": [recipient],
            "subject": subject,
            "html": html,
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send the daily markets brief email.")
    parser.add_argument("--dry-run", action="store_true", help="Render and print HTML, no send.")
    parser.add_argument("--to-file", metavar="PATH", help="Render and save HTML to PATH (no send).")
    parser.add_argument("--force", action="store_true", help="Send even if ENABLE_DAILY_EMAIL is unset.")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    enabled = os.environ.get("ENABLE_DAILY_EMAIL", "").lower() in ("true", "1", "yes")
    subject, html = render_email()

    if args.dry_run:
        print(f"Subject: {subject}")
        print()
        print(html)
        return 0

    if args.to_file:
        Path(args.to_file).write_text(html, encoding="utf-8")
        log.info("Wrote %d bytes to %s. Subject: %s", len(html), args.to_file, subject)
        return 0

    if not enabled and not args.force:
        log.info("ENABLE_DAILY_EMAIL is not set; skipping send. Use --force or --dry-run for ad-hoc.")
        return 0

    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    sender = os.environ.get("EMAIL_FROM", "").strip()
    recipient = os.environ.get("EMAIL_TO", "").strip()
    missing = [name for name, val in (
        ("RESEND_API_KEY", api_key),
        ("EMAIL_FROM",     sender),
        ("EMAIL_TO",       recipient),
    ) if not val]
    if missing:
        log.error("Missing required env vars: %s", ", ".join(missing))
        log.error("Sign up at https://resend.com, verify your sending domain, then set the three vars in .env.")
        return 2

    log.info("Sending '%s' from %s to %s …", subject, sender, recipient)
    try:
        result = send_via_resend(subject, html, api_key=api_key, sender=sender, recipient=recipient)
    except httpx.HTTPStatusError as exc:
        log.error("Resend rejected the send: %s — body: %s", exc, exc.response.text[:500])
        return 3
    except Exception as exc:  # noqa: BLE001
        log.error("Email send failed: %s", exc, exc_info=True)
        return 4

    log.info("Sent. Resend id: %s", result.get("id"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
