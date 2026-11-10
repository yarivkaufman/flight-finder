"""Price-history chart: inline SVG step lines (your dates / nearby dates), no JS library."""

from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

W, H = 720, 240
PAD_L, PAD_R, PAD_T, PAD_B = 56, 130, 12, 28  # right pad holds the end labels
SERIES = (("exact_low", "Your dates"), ("flex_low", "Nearby dates"))


def _nice_ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    span = max(hi - lo, 1.0)
    raw = span / n
    mag = 10 ** len(str(int(raw))) / 10
    step = next(s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw)
    ticks, t = [], int(lo // step) * step
    while t <= hi + step / 2:
        if t >= lo - step / 2:
            ticks.append(round(t, 2))
        t += step
    return ticks


def history_svg(rows, tz: str = "America/New_York") -> str:
    """rows: checks ordered by time with exact_low / flex_low. Empty string until 2 checks exist."""
    zone = ZoneInfo(tz)
    series = {key: [(datetime.fromisoformat(r["at"]), r[key]) for r in rows if r[key] is not None]
              for key, _ in SERIES}
    values = [v for pts in series.values() for _, v in pts]
    times = [t for pts in series.values() for t, _ in pts]
    if len(set(times)) < 2 or not values:
        return ""
    t0, t1 = min(times), max(times)
    ticks = _nice_ticks(min(values), max(values))
    lo, hi = min(ticks[0], min(values)), max(ticks[-1], max(values))
    hi = hi if hi > lo else lo + 1
    pw, ph = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(t: datetime) -> float:
        return PAD_L + (t - t0).total_seconds() / max((t1 - t0).total_seconds(), 1) * pw

    def y(v: float) -> float:
        return PAD_T + (hi - v) / (hi - lo) * ph

    out = [f'<svg class="chart" viewBox="0 0 {W} {H}" role="img" '
           f'aria-label="Cheapest price over time, on your dates and on nearby dates">']
    for v in ticks:
        out.append(f'<line class="grid" x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(v):.1f}" y2="{y(v):.1f}"/>'
                   f'<text class="tick" x="{PAD_L - 6}" y="{y(v) + 4:.1f}" text-anchor="end">${v:,.0f}</text>')
    for t, anchor in ((t0, "start"), (t1, "end")):
        out.append(f'<text class="tick" x="{x(t):.1f}" y="{H - 8}" text-anchor="{anchor}">'
                   f'{t.astimezone(zone).strftime("%b %d")}</text>')
    labels = []
    for i, (key, label) in enumerate(SERIES, 1):
        pts = series[key]
        if not pts:
            continue
        line: list[str] = []
        for t, v in pts:  # step line: price holds until the next check
            if line:
                line.append(f"{x(t):.1f},{line[-1].split(',')[1]}")
            line.append(f"{x(t):.1f},{y(v):.1f}")
        if len(line) == 1:
            cx, cy = line[0].split(",")
            out.append(f'<circle class="s{i} dot" cx="{cx}" cy="{cy}" r="4"/>')
        else:
            out.append(f'<polyline class="s{i} line" points="{" ".join(line)}"/>')
        last = pts[-1][1]
        labels.append([y(last), i, f"{label} ${last:,.0f}"])
    labels.sort()
    for j in range(1, len(labels)):  # keep end labels from overlapping
        labels[j][0] = max(labels[j][0], labels[j - 1][0] + 14)
    for ly, i, text in labels:
        out.append(f'<line class="s{i} key" x1="{W - PAD_R + 6}" x2="{W - PAD_R + 16}" '
                   f'y1="{ly:.1f}" y2="{ly:.1f}"/><text class="endlabel" x="{W - PAD_R + 20}" '
                   f'y="{ly + 4:.1f}">{html.escape(text)}</text>')
    out.append("</svg>")
    return "".join(out)
