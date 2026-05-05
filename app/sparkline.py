"""Inline SVG sparkline generator.

Tiny self-contained line chart drawn into a single inline <svg> element.
Used for the 7-day price + dispatch trends in the dashboard.

We deliberately avoid Plotly here — for trivial 7-point sparks, inline SVG
is ~100 bytes per chart and needs no client-side JS. If interactive charts
are wanted later, swap to Plotly at the embed point."""

from __future__ import annotations

DEFAULT_WIDTH = 80
DEFAULT_HEIGHT = 20
DEFAULT_STROKE = "#1F3864"


def render(values: list, width: int = DEFAULT_WIDTH, height: int = DEFAULT_HEIGHT, stroke: str = DEFAULT_STROKE) -> str:
    """Return inline SVG markup. None/missing values are skipped.

    Returns a fallback "no history yet" span if fewer than 2 valid values
    are available — this is normal for the first day or two of operation."""
    cleaned = [float(v) for v in values if v is not None]
    if len(cleaned) < 2:
        return '<span class="sparkline-empty">no history yet</span>'

    lo = min(cleaned)
    hi = max(cleaned)
    span = (hi - lo) or 1.0
    n = len(cleaned) - 1

    pad_y = 1.5  # leaves room for stroke at top/bottom
    plot_h = height - 2 * pad_y
    pts = []
    for i, v in enumerate(cleaned):
        x = (i / n) * (width - 2)
        y = (height - pad_y) - ((v - lo) / span) * plot_h
        pts.append(f"{x + 1:.1f},{y:.1f}")
    polyline = " ".join(pts)

    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'xmlns="http://www.w3.org/2000/svg" class="sparkline" '
        f'role="img" aria-label="7-day trend, latest {cleaned[-1]:.2f}">'
        f'<polyline fill="none" stroke="{stroke}" stroke-width="1.5" '
        f'stroke-linecap="round" stroke-linejoin="round" points="{polyline}"/>'
        f"</svg>"
    )
