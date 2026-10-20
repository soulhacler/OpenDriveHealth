"""Local history: keeps a snapshot per check so we can chart trends and alert
on *changes* (a new bad sector matters far more than an old, stable one)."""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .health import temperature_status
from .models import Drive, DriveKind, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drive_key TEXT NOT NULL,
    ts REAL NOT NULL,
    model TEXT, serial TEXT,
    status TEXT, health INTEGER, temperature INTEGER, power_on_hours INTEGER,
    reallocated INTEGER, pending INTEGER, uncorrectable INTEGER,
    media_errors INTEGER, host_writes_bytes INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snapshots_key_ts ON snapshots(drive_key, ts);
"""

# Snapshot fields that count physical defects; any increase triggers an alert.
DEFECT_FIELDS = {
    "reallocated": "reallocated sectors",
    "pending": "pending sectors",
    "uncorrectable": "uncorrectable sectors",
    "media_errors": "NVMe media errors",
}

# Ignore tiny drops (e.g. an SSD wearing by 1%) to keep alerts meaningful.
HEALTH_DROP_ALERT = 3


def default_db_path() -> Path:
    if os.environ.get("ODH_DB"):
        return Path(os.environ["ODH_DB"])
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
        return base / "OpenDriveHealth" / "history.sqlite3"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "OpenDriveHealth" / "history.sqlite3"
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "opendrivehealth" / "history.sqlite3"


@dataclass
class Snapshot:
    drive_key: str
    ts: float
    model: str
    serial: str
    status: str
    health: Optional[int]
    temperature: Optional[int]
    power_on_hours: Optional[int]
    reallocated: Optional[int]
    pending: Optional[int]
    uncorrectable: Optional[int]
    media_errors: Optional[int]
    host_writes_bytes: Optional[int]


@dataclass
class Alert:
    severity: Status
    drive: str
    message: str


def _count(drive: Drive, attr_id: int) -> Optional[int]:
    a = drive.attribute(attr_id)
    if a is None:
        return None
    head = a.raw_string.split(" ")[0] if a.raw_string else ""
    return int(head) if head.isdigit() else a.raw


def snapshot_of(drive: Drive, ts: Optional[float] = None) -> Snapshot:
    a = drive.assessment
    nvme = drive.kind == DriveKind.NVME
    return Snapshot(
        drive_key=drive.key,
        ts=ts if ts is not None else time.time(),
        model=drive.model,
        serial=drive.serial,
        status=a.status.value if a else Status.UNKNOWN.value,
        health=a.health if a else None,
        temperature=drive.temperature_c,
        power_on_hours=drive.power_on_hours,
        reallocated=None if nvme else _count(drive, 5),
        pending=None if nvme else _count(drive, 197),
        uncorrectable=None if nvme else _count(drive, 198),
        media_errors=drive.nvme_log.get("media_errors") if nvme else None,
        host_writes_bytes=drive.host_writes_bytes,
    )


class History:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "History":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def last(self, drive_key: str) -> Optional[Snapshot]:
        rows = self.series(drive_key, limit=1)
        return rows[-1] if rows else None

    def series(self, drive_key: str, limit: int = 1000) -> List[Snapshot]:
        cur = self.conn.execute(
            "SELECT drive_key, ts, model, serial, status, health, temperature, "
            "power_on_hours, reallocated, pending, uncorrectable, media_errors, "
            "host_writes_bytes FROM snapshots WHERE drive_key = ? "
            "ORDER BY ts DESC LIMIT ?",
            (drive_key, limit),
        )
        return [Snapshot(*row) for row in reversed(cur.fetchall())]

    def drives(self) -> List[str]:
        cur = self.conn.execute("SELECT DISTINCT drive_key FROM snapshots ORDER BY drive_key")
        return [r[0] for r in cur.fetchall()]

    def record(self, drive: Drive, ts: Optional[float] = None) -> List[Alert]:
        """Store a snapshot and return alerts for anything that got worse."""
        if drive.assessment is None or drive.assessment.status == Status.UNKNOWN:
            return []
        snap = snapshot_of(drive, ts)
        prev = self.last(snap.drive_key)
        self.conn.execute(
            "INSERT INTO snapshots (drive_key, ts, model, serial, status, health, "
            "temperature, power_on_hours, reallocated, pending, uncorrectable, "
            "media_errors, host_writes_bytes) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (snap.drive_key, snap.ts, snap.model, snap.serial, snap.status,
             snap.health, snap.temperature, snap.power_on_hours, snap.reallocated,
             snap.pending, snap.uncorrectable, snap.media_errors,
             snap.host_writes_bytes),
        )
        self.conn.commit()
        return compare(prev, snap, drive)


def compare(prev: Optional[Snapshot], cur: Snapshot, drive: Drive) -> List[Alert]:
    name = f"{cur.model or drive.device} ({drive.device})"
    alerts: List[Alert] = []
    if temperature_status(drive) in (Status.CAUTION, Status.BAD):
        alerts.append(Alert(temperature_status(drive), name,
                            f"Temperature is {cur.temperature} C."))
    if prev is None:
        if cur.status in (Status.CAUTION.value, Status.BAD.value):
            alerts.append(Alert(Status(cur.status), name,
                                f"Drive status is {cur.status} (health {cur.health}%)."))
        return alerts
    for field_name, label in DEFECT_FIELDS.items():
        before, after = getattr(prev, field_name), getattr(cur, field_name)
        if before is not None and after is not None and after > before:
            alerts.append(Alert(Status.BAD if field_name != "reallocated" else Status.CAUTION,
                                name, f"{after - before} new {label} (now {after})."))
    if Status(cur.status).rank > Status(prev.status).rank:
        alerts.append(Alert(Status(cur.status), name,
                            f"Status changed from {prev.status} to {cur.status}."))
    if (prev.health is not None and cur.health is not None
            and prev.health - cur.health >= HEALTH_DROP_ALERT):
        alerts.append(Alert(Status.CAUTION, name,
                            f"Health dropped from {prev.health}% to {cur.health}%."))
    return alerts
