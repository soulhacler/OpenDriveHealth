import json

from opendrivehealth.cli import main


def test_list_demo(capsys):
    assert main(["--demo", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "ST2000DM001" in out and "Bad" in out and "NVMe" in out


def test_show_json(capsys):
    assert main(["show", "nvme0", "--demo", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["assessment"]["status"] == "Good"


def test_check_exit_code_reflects_worst(tmp_path, capsys):
    assert main(["check", "--demo", "--db", str(tmp_path / "h.db"), "-q"]) == 2
    assert "Bad" in capsys.readouterr().out
    assert main(["history", "--db", str(tmp_path / "h.db")]) == 0


def test_json_file(tmp_path, capsys):
    from opendrivehealth.core import DEMO_DIR
    assert main(["check", "--json-file", str(DEMO_DIR / "hdd_healthy.json"),
                 "--db", str(tmp_path / "h.db"), "-q"]) == 0


def test_missing_smartctl(monkeypatch, capsys):
    monkeypatch.setattr("opendrivehealth.smartctl.find_smartctl", lambda: None)
    assert main(["list"]) == 3
    assert "smartctl was not found" in capsys.readouterr().err
