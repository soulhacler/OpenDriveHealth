# Contributing to OpenDriveHealth

Thanks for helping! Every kind of contribution is welcome, and you don't need
to write code to make a big difference.

## Share drive data (no coding needed)

The health score is only as good as the real-world data behind it. If you
have a drive that is failing or has failed, or a drive whose score looks
wrong, please share its data:

1. Open an Administrator terminal (Windows) or use `sudo` (Linux/macOS).
2. Run `odh list` to find the drive's number.
3. Run `odh dump <number> -o drive.json`. Serial numbers and other unique IDs
   are removed automatically. Open the file to check if you like.
4. Open a [drive report](../../issues/new?template=drive_report.yml) and attach
   `drive.json`. Tell us whether the drive is fine, struggling or dead.

## Report bugs and ideas

Use the [issue templates](../../issues/new/choose). For anything that looks
wrong, a screenshot and your OS and version help most.

## Code

```sh
git clone https://github.com/soulhacler/OpenDriveHealth
cd OpenDriveHealth
pip install -e .[test]
pytest
odh-gui --demo        # try the GUI with sample drives
```

Project layout:

| Path | What |
|---|---|
| `src/opendrivehealth/smartctl.py` | Runs `smartctl --json` and parses it |
| `src/opendrivehealth/health.py` | Status and health % engine (`WEIGHTS` are the scoring rules) |
| `src/opendrivehealth/history.py` | SQLite snapshots and change alerts |
| `src/opendrivehealth/cli.py` | The `odh` command |
| `src/opendrivehealth/gui.py`, `widgets.py` | Desktop app (Tkinter, no extra dependencies) |
| `src/opendrivehealth/demo/` | Sample `smartctl` dumps, used by `--demo` and the tests |
| `packaging/windows/` | Windows `.exe` build |

Guidelines:

- **No new runtime dependencies** unless there's a strong reason. Plain Python
  and smartmontools keep the app easy to install and package.
- **Scoring changes need evidence.** Add a test, ideally using a real drive
  dump (anonymised with `odh dump`), that shows the old score was wrong.
- **Keep the GUI looking the same on all platforms.** Use the colours and
  widgets in `widgets.py` rather than native ttk styling.
- Run `pytest` before opening a pull request. CI runs it on Windows, macOS and
  Linux and builds the Windows `.exe`.

## Releases

1. Bump `__version__` in `src/opendrivehealth/__init__.py` and add a
   `CHANGELOG.md` entry.
2. On GitHub, open **Actions → Windows build → Run workflow** and enter the
   version (e.g. `0.2.0`). It builds and tests the `.exe`, tags `v0.2.0` and
   publishes the release with `OpenDriveHealth-v0.2.0-windows.zip` attached.
   Pushing a `v*` tag yourself does the same.
