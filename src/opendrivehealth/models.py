"""Plain data structures shared by the backend, health engine, CLI and GUI."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class Status(str, Enum):
    """Overall drive status, CrystalDiskInfo style."""

    GOOD = "Good"
    CAUTION = "Caution"
    BAD = "Bad"
    UNKNOWN = "Unknown"

    @property
    def rank(self) -> int:
        return {"Good": 0, "Unknown": 1, "Caution": 2, "Bad": 3}[self.value]

    @staticmethod
    def worst(*statuses: "Status") -> "Status":
        return max(statuses, key=lambda s: s.rank, default=Status.UNKNOWN)


class DriveKind(str, Enum):
    HDD = "HDD"
    SSD = "SSD"
    NVME = "NVMe"
    UNKNOWN = "Unknown"


@dataclass
class Attribute:
    """One ATA S.M.A.R.T. attribute (or a synthetic one for NVMe)."""

    id: int
    name: str
    value: Optional[int]  # normalised current value
    worst: Optional[int]
    threshold: Optional[int]
    raw: int
    raw_string: str = ""
    prefailure: bool = False
    status: Status = Status.GOOD
    note: str = ""


@dataclass
class Finding:
    """A single explanation of why the health score dropped."""

    severity: Status
    message: str
    penalty: float = 0.0


@dataclass
class Assessment:
    status: Status
    health: Optional[int]  # 0..100, HDD Sentinel-style percentage
    findings: List[Finding] = field(default_factory=list)
    wear_remaining: Optional[int] = None  # SSD/NVMe endurance left, percent


@dataclass
class Drive:
    device: str  # e.g. /dev/sda, /dev/nvme0
    device_type: str = ""  # smartctl -d type
    protocol: str = ""  # ATA, NVMe, SCSI
    kind: DriveKind = DriveKind.UNKNOWN
    model: str = ""
    serial: str = ""
    firmware: str = ""
    capacity_bytes: Optional[int] = None
    rotation_rpm: Optional[int] = None
    interface: str = ""
    smart_supported: Optional[bool] = None
    smart_enabled: Optional[bool] = None
    smart_passed: Optional[bool] = None
    temperature_c: Optional[int] = None
    power_on_hours: Optional[int] = None
    power_cycles: Optional[int] = None
    host_reads_bytes: Optional[int] = None
    host_writes_bytes: Optional[int] = None
    attributes: List[Attribute] = field(default_factory=list)
    nvme_log: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)
    assessment: Optional[Assessment] = None

    @property
    def key(self) -> str:
        """Stable identity across reboots (device names can change)."""
        if self.serial:
            return f"{self.model}|{self.serial}"
        return self.device

    def attribute(self, attr_id: int) -> Optional[Attribute]:
        for a in self.attributes:
            if a.id == attr_id:
                return a
        return None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
