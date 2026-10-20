from opendrivehealth.core import load_drives
from opendrivehealth.history import History
from opendrivehealth.models import Status


def _hdd():
    return next(d for d in load_drives(demo=True) if d.device == "/dev/sda")


def test_first_record_of_good_drive_has_no_alerts(tmp_path):
    with History(tmp_path / "h.db") as h:
        assert h.record(_hdd(), ts=1) == []
        assert len(h.series(_hdd().key)) == 1


def test_new_bad_sectors_alert(tmp_path):
    with History(tmp_path / "h.db") as h:
        h.record(_hdd(), ts=1)
        d = _hdd()
        d.attribute(5).raw, d.attribute(5).raw_string = 8, "8"
        d.attribute(197).raw, d.attribute(197).raw_string = 2, "2"
        from opendrivehealth.health import assess
        assess(d)
        alerts = h.record(d, ts=2)
        msgs = [a.message for a in alerts]
        assert any("8 new reallocated" in m for m in msgs)
        assert any(a.severity == Status.BAD and "pending" in a.message for a in alerts)
        assert any("Status changed from Good to Caution" in m for m in msgs)
        assert [s.ts for s in h.series(d.key)] == [1, 2]


def test_stable_problem_does_not_re_alert(tmp_path):
    failing = next(d for d in load_drives(demo=True) if d.device == "/dev/sdb")
    with History(tmp_path / "h.db") as h:
        first = h.record(failing, ts=1)
        assert first and first[0].severity == Status.BAD
        assert h.record(failing, ts=2) == []


def test_hot_drive_alerts(tmp_path):
    d = _hdd()
    d.temperature_c = 62
    with History(tmp_path / "h.db") as h:
        alerts = h.record(d, ts=1)
    assert alerts[0].severity == Status.BAD and "62 C" in alerts[0].message
