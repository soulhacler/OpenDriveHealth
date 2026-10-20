"""Human-friendly formatting helpers."""

from __future__ import annotations

from typing import Optional


def size(n: Optional[int], si: bool = True) -> str:
    """Capacity as printed on the box (decimal units) by default."""
    if n is None:
        return "-"
    step = 1000.0 if si else 1024.0
    units = ["B", "KB", "MB", "GB", "TB", "PB"] if si else ["B", "KiB", "MiB", "GiB", "TiB", "PiB"]
    v = float(n)
    for u in units:
        if v < step or u == units[-1]:
            return f"{v:.0f} {u}" if u == "B" else f"{v:.1f} {u}"
        v /= step
    return f"{n} B"


def hours(h: Optional[int]) -> str:
    if h is None:
        return "-"
    days, rem = divmod(int(h), 24)
    years, days = divmod(days, 365)
    parts = []
    if years:
        parts.append(f"{years}y")
    if years or days:
        parts.append(f"{days}d")
    parts.append(f"{rem}h")
    return f"{h:,} h ({' '.join(parts)})"


def opt(v, suffix: str = "") -> str:
    return "-" if v is None else f"{v}{suffix}"
