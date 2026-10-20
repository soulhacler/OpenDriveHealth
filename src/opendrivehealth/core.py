"""High-level entry points shared by the CLI and GUI."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from . import smartctl
from .health import assess
from .models import Drive

DEMO_DIR = Path(__file__).parent / "demo"


def demo_drives() -> List[Drive]:
    return [smartctl.load_json(str(p)) for p in sorted(DEMO_DIR.glob("*.json"))]


def load_drives(demo: bool = False, json_files: Optional[List[str]] = None,
                wake: bool = False) -> List[Drive]:
    """Discover drives (or load saved dumps) and assess each one."""
    if json_files:
        drives = [smartctl.load_json(p) for p in json_files]
    elif demo:
        drives = demo_drives()
    else:
        drives = smartctl.read_all(wake=wake)
    for d in drives:
        assess(d)
    return drives


def select(drives: List[Drive], ident: str) -> Optional[Drive]:
    """Find a drive by index (0-based), device path or serial."""
    if ident.isdigit() and int(ident) < len(drives):
        return drives[int(ident)]
    for d in drives:
        if ident in (d.device, d.serial) or d.device.endswith("/" + ident):
            return d
    return None
