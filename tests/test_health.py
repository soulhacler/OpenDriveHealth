import copy
import json

import pytest

from opendrivehealth import smartctl
from opendrivehealth.core import DEMO_DIR, load_drives, select
from opendrivehealth.health import assess, temperature_status
from opendrivehealth.models import DriveKind, Status


def raw(name):
    return json.loads((DEMO_DIR / f"{name}.json").read_text())


def drive(name, mutate=None):
    data = raw(name)
    if mutate:
        mutate(data)
    d = smartctl.parse(data)
    assess(d)
    return d


def set_attr(data, attr_id, **kw):
    for row in data["ata_smart_attributes"]["table"]:
        if row["id"] == attr_id:
            for k, v in kw.items():
                if k == "raw":
                    row["raw"] = {"value": v, "string": str(v)}
                else:
                    row[k] = v
            return
    raise KeyError(attr_id)


def test_parse_hdd():
    d = drive("hdd_healthy")
    assert d.kind == DriveKind.HDD
    assert d.model == "WDC WD40EFRX-68N32N0"
    assert d.capacity_bytes == 4000787030016
    assert d.temperature_c == 34
    assert d.power_on_hours == 21873
    assert d.attribute(5).name == "Reallocated Sector Ct"


def test_healthy_hdd_is_good_100():
    a = drive("hdd_healthy").assessment
    assert a.status == Status.GOOD
    assert a.health == 100
    assert a.findings == []


def test_single_reallocated_sector_is_caution():
    d = drive("hdd_healthy", lambda x: set_attr(x, 5, raw=1))
    assert d.assessment.status == Status.CAUTION
    assert 90 <= d.assessment.health < 100
    assert d.attribute(5).status == Status.CAUTION


def test_more_sectors_score_lower():
    scores = [drive("hdd_healthy", lambda x, n=n: set_attr(x, 5, raw=n)).assessment.health
              for n in (1, 10, 100, 1000)]
    assert scores == sorted(scores, reverse=True)
    assert len(set(scores)) == 4


def test_threshold_crossed_is_bad():
    d = drive("hdd_healthy", lambda x: set_attr(x, 5, value=140))
    assert d.assessment.status == Status.BAD
    assert d.assessment.health <= 10
    assert d.attribute(5).status == Status.BAD


def test_smart_failed_is_bad():
    d = drive("hdd_healthy", lambda x: x["smart_status"].update(passed=False))
    assert d.assessment.status == Status.BAD
    assert "FAILING" in d.assessment.findings[0].message


def test_failing_hdd_despite_smart_passed():
    d = drive("hdd_failing")
    assert d.smart_passed is True
    assert d.assessment.status == Status.BAD
    assert d.assessment.health < 25
    # Command_Timeout raw is packed; only the low 16 bits count.
    assert any("1 command timeouts" in f.message for f in d.assessment.findings)
    # Raw strings like "47 (Min/Max 19/59)" decode to the first number.
    assert d.temperature_c == 47


def test_pending_and_offline_not_double_counted():
    d = drive("hdd_failing")
    offline = next(f for f in d.assessment.findings if "offline" in f.message)
    assert offline.penalty == 0


def test_crc_errors_are_informational():
    d = drive("hdd_healthy", lambda x: set_attr(x, 199, raw=50))
    assert d.assessment.status == Status.GOOD
    assert d.assessment.health == 100
    assert any("cable" in f.message for f in d.assessment.findings)


def test_sata_ssd_wear():
    d = drive("ssd_sata")
    assert d.kind == DriveKind.SSD
    assert d.assessment.wear_remaining == 88
    assert d.assessment.health == 88
    assert d.host_writes_bytes == 61483920112 * 512


def test_worn_ssd_is_caution():
    d = drive("ssd_sata", lambda x: set_attr(x, 177, value=5))
    assert d.assessment.status == Status.CAUTION


def test_nvme_healthy():
    d = drive("nvme")
    assert d.kind == DriveKind.NVME
    assert d.assessment.status == Status.GOOD
    assert d.assessment.wear_remaining == 97
    assert d.host_writes_bytes == 41829211 * 512000
    assert d.attribute(0x0D).raw == 57


@pytest.mark.parametrize("bit,expected", [(0x01, Status.BAD), (0x02, Status.CAUTION),
                                          (0x04, Status.BAD), (0x08, Status.BAD)])
def test_nvme_critical_warning(bit, expected):
    d = drive("nvme", lambda x: x["nvme_smart_health_information_log"].update(critical_warning=bit))
    assert d.assessment.status == expected


def test_nvme_media_errors():
    d = drive("nvme", lambda x: x["nvme_smart_health_information_log"].update(media_errors=3))
    assert d.assessment.status == Status.CAUTION
    assert d.assessment.health < 97


def test_nvme_spare_below_threshold():
    d = drive("nvme", lambda x: x["nvme_smart_health_information_log"].update(available_spare=5))
    assert d.assessment.status == Status.BAD


def test_no_smart_is_unknown():
    d = smartctl.parse({"device": {"name": "/dev/sdx", "protocol": "ATA"},
                        "smart_support": {"available": False},
                        "temperature": {"current": 0}})
    assert d.temperature_c is None
    assert assess(d).status == Status.UNKNOWN
    assert d.errors


def test_temperature_limits():
    d = drive("hdd_healthy")
    assert temperature_status(d) == Status.GOOD
    d.temperature_c = 55
    assert temperature_status(d) == Status.CAUTION
    d.temperature_c = 61
    assert temperature_status(d) == Status.BAD


def test_demo_and_select():
    drives = load_drives(demo=True)
    assert len(drives) == 4
    assert select(drives, "nvme0").kind == DriveKind.NVME
    assert select(drives, "/dev/sda").model.startswith("WDC")
    assert select(drives, "1") is drives[1]
    assert select(drives, "nope") is None
