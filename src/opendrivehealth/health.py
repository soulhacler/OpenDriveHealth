"""Health engine.

Produces two complementary views of a drive:

* a **status** (Good / Caution / Bad) using the same rules of thumb as
  CrystalDiskInfo: anything at or below its vendor threshold is Bad, any
  remapped / pending / uncorrectable sector is Caution;
* a **health percentage** in the spirit of HDD Sentinel: start at 100 % and
  deduct for each sign of degradation, with diminishing returns so that the
  first few bad sectors matter most. Deductions compound multiplicatively. For SSDs the result is further scaled by
  the remaining rated endurance.

The exact HDD Sentinel formula is proprietary; this one is deliberately
simple and every deduction is reported as a ``Finding`` so users can see
exactly why a drive scored what it did. Tweaks are welcome -- see WEIGHTS.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from .models import Assessment, Attribute, Drive, DriveKind, Finding, Status


@dataclass(frozen=True)
class Rule:
    attr_id: int
    label: str
    weight: float  # points lost for the first event
    cap: float  # maximum points this rule can remove
    status: Status  # status raised when raw > 0
    advice: str = ""
    raw: Callable[[Attribute], int] = lambda a: a.raw


def _low16(a: Attribute) -> int:
    return a.raw & 0xFFFF


def _first_number(a: Attribute) -> int:
    head = a.raw_string.split(" ")[0] if a.raw_string else ""
    return int(head) if head.isdigit() else a.raw


# ATA attributes that indicate physical degradation.
WEIGHTS: Tuple[Rule, ...] = (
    Rule(5, "reallocated sectors", 6, 70, Status.CAUTION,
         "The drive has remapped bad sectors. Back up and watch for growth.",
         _first_number),
    Rule(197, "pending (unstable) sectors", 8, 70, Status.CAUTION,
         "Sectors could not be read reliably; data in them may be lost.",
         _first_number),
    # 198 usually reports the same sectors as 197, so only the excess counts.
    Rule(198, "offline uncorrectable sectors", 8, 70, Status.CAUTION,
         "Sectors failed the drive's own surface scan.", _first_number),
    Rule(196, "reallocation events", 3, 30, Status.CAUTION, "", _first_number),
    Rule(187, "reported uncorrectable errors", 4, 40, Status.CAUTION,
         "The drive returned read errors it could not correct.", _low16),
    Rule(10, "spin-up retries", 5, 40, Status.CAUTION,
         "The motor struggled to spin up; may indicate mechanical or power issues.",
         _low16),
    Rule(188, "command timeouts", 1, 10, Status.GOOD, "", _low16),
)

# Normalised-value attributes that report remaining SSD life (100 = new).
SSD_LIFE_ATTRS = (231, 233, 202, 169, 177, 173)

# Below this percentage remaining, a worn-out SSD is reported as Caution.
WEAR_CAUTION_PCT = 10

# Health percentages below which the overall status is raised.
CAUTION_BELOW_PCT = 60
BAD_BELOW_PCT = 25


def _penalty(count: int, weight: float, cap: float) -> float:
    if count <= 0:
        return 0.0
    return min(cap, weight * math.log2(1 + count))


def ssd_life_remaining(drive: Drive) -> Optional[int]:
    if drive.kind == DriveKind.NVME:
        used = drive.nvme_log.get("percentage_used")
        return None if used is None else max(0, 100 - int(used))
    if drive.kind != DriveKind.SSD:
        return None
    for aid in SSD_LIFE_ATTRS:
        a = drive.attribute(aid)
        if a and a.value is not None and 0 <= a.value <= 100:
            return a.value
    return None


def _assess_ata(drive: Drive, findings: List[Finding]) -> Status:
    status = Status.GOOD
    for a in drive.attributes:
        if a.threshold and a.value is not None and a.value <= a.threshold:
            a.status = Status.BAD
            a.note = "at or below vendor failure threshold"
            status = Status.BAD
            findings.append(Finding(Status.BAD,
                f"Attribute {a.id:02X} ({a.name}) is at or below its failure "
                f"threshold ({a.value} <= {a.threshold}).", 0))
    for rule in WEIGHTS:
        a = drive.attribute(rule.attr_id)
        if a is None:
            continue
        count = rule.raw(a)
        if count <= 0:
            continue
        scored = count
        if rule.attr_id == 198 and drive.attribute(197):
            scored = max(0, count - _first_number(drive.attribute(197)))
        p = _penalty(scored, rule.weight, rule.cap)
        if a.status == Status.GOOD:
            a.status = rule.status
        status = Status.worst(status, rule.status)
        msg = f"{count} {rule.label}."
        if rule.advice:
            msg += " " + rule.advice
        findings.append(Finding(rule.status, msg, p))
    crc = drive.attribute(199)
    if crc and _low16(crc) > 0:
        findings.append(Finding(Status.GOOD,
            f"{_low16(crc)} interface CRC errors. These are usually caused by a "
            "bad SATA cable or connector, not the drive itself.", 0))
    return status


_NVME_CRIT_BITS = (
    (0x01, Status.BAD, "available spare capacity is below threshold"),
    (0x02, Status.CAUTION, "temperature is outside the safe range"),
    (0x04, Status.BAD, "NVM subsystem reliability is degraded"),
    (0x08, Status.BAD, "media has been placed in read-only mode"),
    (0x10, Status.CAUTION, "volatile memory backup device has failed"),
    (0x20, Status.BAD, "persistent memory region is read-only or unreliable"),
)


def _assess_nvme(drive: Drive, findings: List[Finding]) -> Status:
    log = drive.nvme_log
    status = Status.GOOD
    crit = int(log.get("critical_warning", 0))
    for bit, sev, text in _NVME_CRIT_BITS:
        if crit & bit:
            status = Status.worst(status, sev)
            findings.append(Finding(sev, f"Critical warning: {text}.",
                                    40 if sev == Status.BAD else 0))
    spare, spare_th = log.get("available_spare"), log.get("available_spare_threshold")
    if spare is not None and spare_th is not None and spare < spare_th and not crit & 0x01:
        status = Status.BAD
        findings.append(Finding(Status.BAD,
            f"Available spare {spare}% is below threshold {spare_th}%.", 40))
    media = int(log.get("media_errors", 0))
    if media > 0:
        status = Status.worst(status, Status.CAUTION)
        findings.append(Finding(Status.CAUTION,
            f"{media} media and data integrity errors (unrecovered data).",
            _penalty(media, 8, 70)))
    for a in drive.attributes:
        if a.id == 0x01 and a.raw:
            a.status = Status.BAD if crit & 0x2D else Status.CAUTION
        elif a.id == 0x03 and spare is not None and spare_th is not None and spare < spare_th:
            a.status = Status.BAD
        elif a.id == 0x0E and a.raw:
            a.status = Status.CAUTION
    return status


def assess(drive: Drive) -> Assessment:
    findings: List[Finding] = []

    if not drive.attributes and drive.smart_passed is None:
        result = Assessment(Status.UNKNOWN, None, [Finding(Status.UNKNOWN,
            "No S.M.A.R.T. data available for this device.")])
        drive.assessment = result
        return result

    if drive.kind == DriveKind.NVME:
        status = _assess_nvme(drive, findings)
    else:
        status = _assess_ata(drive, findings)

    if drive.smart_passed is False:
        status = Status.BAD
        findings.insert(0, Finding(Status.BAD,
            "The drive's own S.M.A.R.T. self-assessment reports FAILING. "
            "Back up your data immediately.", 0))

    # Deductions compound (each one removes a share of what is left), so a
    # drive with several problems approaches 0 % without hitting it outright.
    score = 100.0
    for f in findings:
        score *= 1 - f.penalty / 100.0
    # Status escalation looks at damage only; normal SSD wear is handled below
    # and on its own never makes a drive "Bad".
    if score < BAD_BELOW_PCT:
        status = Status.BAD
    elif score < CAUTION_BELOW_PCT:
        status = Status.worst(status, Status.CAUTION)
    life = ssd_life_remaining(drive)
    if life is not None:
        if life < 100:
            findings.append(Finding(Status.GOOD,
                f"{100 - life}% of rated write endurance used.", 0))
        if life <= WEAR_CAUTION_PCT:
            status = Status.worst(status, Status.CAUTION)
            findings.append(Finding(Status.CAUTION,
                f"Only {life}% rated endurance remaining. Plan a replacement.", 0))
        score *= life / 100.0
    if status == Status.BAD:
        score = min(score, 10.0)
    score = max(0.0, min(100.0, score))

    findings.sort(key=lambda f: -f.severity.rank)
    result = Assessment(status, int(round(score)), findings, life)
    drive.assessment = result
    return result


# Temperature limits (caution, bad) in degrees C. HDDs are happiest below ~45 C.
TEMP_LIMITS = {
    DriveKind.HDD: (50, 60),
    DriveKind.SSD: (60, 70),
    DriveKind.NVME: (70, 80),
    DriveKind.UNKNOWN: (55, 65),
}


def temperature_status(drive: Drive) -> Status:
    if drive.temperature_c is None:
        return Status.UNKNOWN
    caution, bad = TEMP_LIMITS[drive.kind]
    if drive.temperature_c >= bad:
        return Status.BAD
    if drive.temperature_c >= caution:
        return Status.CAUTION
    return Status.GOOD
