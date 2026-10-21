"""Backend: talks to smartmontools' ``smartctl`` and parses its JSON output.

smartmontools is the de-facto open-source S.M.A.R.T. toolkit and runs on
Linux, Windows, macOS and the BSDs, so building on top of it gives us broad
hardware support (SATA, SAS, NVMe, many USB bridges, RAID controllers)
without writing any low-level ioctl code ourselves.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import Attribute, Drive, DriveKind

# smartctl exit status bits (see `man smartctl`, RETURN VALUES).
EXIT_CMDLINE = 1 << 0
EXIT_OPEN_FAILED = 1 << 1  # also returned when -n standby skips a sleeping disk
EXIT_CMD_FAILED = 1 << 2
EXIT_DISK_FAILING = 1 << 3
EXIT_PREFAIL_BELOW_THRESH = 1 << 4
EXIT_USAGE_BELOW_THRESH_PAST = 1 << 5
EXIT_ERROR_LOG = 1 << 6
EXIT_SELFTEST_ERRORS = 1 << 7

NVME_DATA_UNIT_BYTES = 512 * 1000

_WINDOWS_CANDIDATES = [
    r"C:\Program Files\smartmontools\bin\smartctl.exe",
    r"C:\Program Files (x86)\smartmontools\bin\smartctl.exe",
]
_UNIX_CANDIDATES = [
    "/usr/sbin/smartctl",
    "/usr/local/sbin/smartctl",
    "/opt/homebrew/bin/smartctl",
    "/usr/local/bin/smartctl",
]


class SmartctlError(RuntimeError):
    pass


def _bundled_smartctl() -> Optional[str]:
    """smartctl shipped inside a PyInstaller build (see packaging/windows)."""
    if not getattr(sys, "frozen", False):
        return None
    name = "smartctl.exe" if sys.platform == "win32" else "smartctl"
    for base in (getattr(sys, "_MEIPASS", ""), os.path.dirname(sys.executable)):
        candidate = Path(base) / "smartmontools" / name
        if base and candidate.is_file():
            return str(candidate)
    return None


def find_smartctl() -> Optional[str]:
    env = os.environ.get("ODH_SMARTCTL")
    if env:
        return env
    bundled = _bundled_smartctl()
    if bundled:
        return bundled
    found = shutil.which("smartctl")
    if found:
        return found
    candidates = _WINDOWS_CANDIDATES if sys.platform == "win32" else _UNIX_CANDIDATES
    for c in candidates:
        if Path(c).is_file():
            return c
    return None


def _run(args: List[str], timeout: int = 60) -> Tuple[Dict[str, Any], int]:
    exe = find_smartctl()
    if not exe:
        raise SmartctlError(
            "smartctl was not found. Install smartmontools "
            "(https://www.smartmontools.org) or set ODH_SMARTCTL to its path."
        )
    kwargs: Dict[str, Any] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW: no console flash in the GUI
    try:
        proc = subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout, **kwargs
        )
    except subprocess.TimeoutExpired as e:
        raise SmartctlError(f"smartctl timed out: {' '.join(args)}") from e
    try:
        data = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError as e:
        raise SmartctlError(
            "smartctl did not return JSON; smartmontools 7.0 or newer is required"
        ) from e
    return data, proc.returncode


def _messages(data: Dict[str, Any]) -> List[str]:
    return [m.get("string", "") for m in data.get("smartctl", {}).get("messages", [])]


def scan() -> List[Tuple[str, str]]:
    """Return ``[(device, type), ...]`` for every drive smartctl can open."""
    data, _ = _run(["--scan-open", "--json"])
    out = []
    for d in data.get("devices", []):
        if d.get("open_error"):
            continue
        out.append((d["name"], d.get("type", "")))
    return out


def _query(device: str, device_type: str = "", wake: bool = False) -> Tuple[Dict[str, Any], int]:
    args = ["--all", "--json"]
    if not wake:
        args += ["--nocheck=standby"]
    if device_type:
        args += ["-d", device_type]
    args.append(device)
    return _run(args)


def raw_json(device: str, device_type: str = "", wake: bool = False) -> Dict[str, Any]:
    """The unparsed ``smartctl --all --json`` output for one device."""
    return _query(device, device_type, wake)[0]


# Fields that identify a specific physical drive. Removed before sharing.
IDENTIFYING_KEYS = ("serial_number", "wwn", "logical_unit_id", "nvme_ieee_oui_identifier",
                    "eui64", "nguid", "scsi_serial_number")


def anonymize(data: Any) -> Any:
    """Copy of a smartctl dump with serial numbers and unique IDs redacted."""
    if isinstance(data, dict):
        return {k: ("REDACTED" if k in IDENTIFYING_KEYS else anonymize(v))
                for k, v in data.items()}
    if isinstance(data, list):
        return [anonymize(v) for v in data]
    return data


def read_drive(device: str, device_type: str = "", wake: bool = False) -> Drive:
    """Query one device. By default sleeping disks are not spun up."""
    data, rc = _query(device, device_type, wake)
    drive = parse(data, device=device, device_type=device_type)
    if rc & (EXIT_CMDLINE | EXIT_OPEN_FAILED):
        msgs = _messages(data) or [f"smartctl exited with status {rc}"]
        if any("STANDBY" in m.upper() or "SLEEP" in m.upper() for m in msgs):
            drive.errors.append("Drive is in standby; skipped to avoid spinning it up.")
        else:
            drive.errors.extend(msgs)
            if any("permission" in m.lower() for m in msgs):
                drive.errors.append(
                    "Run as Administrator / root to read S.M.A.R.T. data."
                )
    return drive


def read_all(wake: bool = False) -> List[Drive]:
    return [read_drive(dev, typ, wake=wake) for dev, typ in scan()]


def load_json(path: str) -> Drive:
    """Build a Drive from a saved ``smartctl --json`` dump (demo / bug reports)."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return parse(data)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _nvme_attrs(log: Dict[str, Any]) -> List[Attribute]:
    """Present the NVMe health log as an attribute table, like CrystalDiskInfo."""
    rows = [
        (0x01, "Critical Warning", "critical_warning"),
        (0x02, "Composite Temperature (C)", "temperature"),
        (0x03, "Available Spare (%)", "available_spare"),
        (0x04, "Available Spare Threshold (%)", "available_spare_threshold"),
        (0x05, "Percentage Used (%)", "percentage_used"),
        (0x06, "Data Units Read", "data_units_read"),
        (0x07, "Data Units Written", "data_units_written"),
        (0x08, "Host Read Commands", "host_reads"),
        (0x09, "Host Write Commands", "host_writes"),
        (0x0A, "Controller Busy Time", "controller_busy_time"),
        (0x0B, "Power Cycles", "power_cycles"),
        (0x0C, "Power On Hours", "power_on_hours"),
        (0x0D, "Unsafe Shutdowns", "unsafe_shutdowns"),
        (0x0E, "Media and Data Integrity Errors", "media_errors"),
        (0x0F, "Error Log Entries", "num_err_log_entries"),
    ]
    attrs = []
    for aid, name, key in rows:
        if key not in log:
            continue
        raw = int(log[key])
        attrs.append(
            Attribute(id=aid, name=name, value=None, worst=None, threshold=None,
                      raw=raw, raw_string=str(raw))
        )
    return attrs


def _ata_attrs(table: List[Dict[str, Any]]) -> List[Attribute]:
    attrs = []
    for row in table:
        raw = row.get("raw", {})
        attrs.append(
            Attribute(
                id=int(row["id"]),
                name=row.get("name", "Unknown_Attribute").replace("_", " "),
                value=row.get("value"),
                worst=row.get("worst"),
                threshold=row.get("thresh"),
                raw=int(raw.get("value", 0)),
                raw_string=str(raw.get("string", raw.get("value", ""))),
                prefailure=bool(row.get("flags", {}).get("prefailure", False)),
            )
        )
    return attrs


def _raw_low(attr: Optional[Attribute]) -> Optional[int]:
    """Many vendors pack extra data in the upper raw bytes; the count is the
    first number of smartctl's decoded raw string (e.g. '34 (Min/Max 20/45)')."""
    if attr is None:
        return None
    head = attr.raw_string.split(" ")[0] if attr.raw_string else ""
    return int(head) if head.isdigit() else attr.raw


def parse(data: Dict[str, Any], device: str = "", device_type: str = "") -> Drive:
    dev = data.get("device", {})
    drive = Drive(
        device=dev.get("name") or device,
        device_type=dev.get("type") or device_type,
        protocol=dev.get("protocol", ""),
        model=data.get("model_name") or data.get("scsi_model_name") or "",
        serial=data.get("serial_number", ""),
        firmware=data.get("firmware_version", ""),
        capacity_bytes=data.get("user_capacity", {}).get("bytes")
        or data.get("nvme_total_capacity"),
        rotation_rpm=data.get("rotation_rate"),
        interface=(data.get("interface_speed", {}).get("current", {}) or {}).get("string", "")
        or (data.get("sata_version", {}) or {}).get("string", ""),
    )
    smart = data.get("smart_support", {})
    drive.smart_supported = smart.get("available")
    drive.smart_enabled = smart.get("enabled")
    if "smart_status" in data:
        drive.smart_passed = data["smart_status"].get("passed")

    drive.temperature_c = data.get("temperature", {}).get("current")
    drive.power_on_hours = data.get("power_on_time", {}).get("hours")
    drive.power_cycles = data.get("power_cycle_count")

    nvme = data.get("nvme_smart_health_information_log")
    if nvme or drive.protocol.upper() == "NVME":
        drive.kind = DriveKind.NVME
        drive.nvme_log = dict(nvme or {})
        drive.attributes = _nvme_attrs(drive.nvme_log)
        if drive.smart_supported is None:
            drive.smart_supported = True
        if drive.smart_enabled is None:
            drive.smart_enabled = True
        if "data_units_read" in drive.nvme_log:
            drive.host_reads_bytes = drive.nvme_log["data_units_read"] * NVME_DATA_UNIT_BYTES
        if "data_units_written" in drive.nvme_log:
            drive.host_writes_bytes = drive.nvme_log["data_units_written"] * NVME_DATA_UNIT_BYTES
        if drive.temperature_c is None:
            drive.temperature_c = drive.nvme_log.get("temperature")
        if drive.power_on_hours is None:
            drive.power_on_hours = drive.nvme_log.get("power_on_hours")
        if drive.power_cycles is None:
            drive.power_cycles = drive.nvme_log.get("power_cycles")
    else:
        drive.attributes = _ata_attrs(data.get("ata_smart_attributes", {}).get("table", []))
        if drive.rotation_rpm and drive.rotation_rpm > 0:
            drive.kind = DriveKind.HDD
        elif drive.rotation_rpm == 0 or any(
            drive.attribute(i) for i in (177, 202, 231, 233)
        ):
            drive.kind = DriveKind.SSD
        elif drive.attributes:
            drive.kind = DriveKind.HDD
        # Fill gaps from attributes when the top-level summary is missing.
        if drive.temperature_c is None:
            drive.temperature_c = _raw_low(drive.attribute(194) or drive.attribute(190))
        if drive.power_on_hours is None:
            drive.power_on_hours = _raw_low(drive.attribute(9))
        if drive.power_cycles is None:
            drive.power_cycles = _raw_low(drive.attribute(12))
        sector = data.get("logical_block_size", 512)
        lbas_w, lbas_r = drive.attribute(241), drive.attribute(242)
        if lbas_w and "lba" in lbas_w.name.lower():
            drive.host_writes_bytes = lbas_w.raw * sector
        if lbas_r and "lba" in lbas_r.name.lower():
            drive.host_reads_bytes = lbas_r.raw * sector

    if drive.temperature_c == 0 and not drive.attributes:
        drive.temperature_c = None  # virtual / non-SMART disks report a bogus 0
    if not drive.attributes and drive.smart_supported is False:
        drive.errors.append("S.M.A.R.T. is not supported by this device or its USB bridge.")
    return drive
