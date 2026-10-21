"""Command-line interface: ``odh``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import __version__, fmt
from .core import load_drives, select
from .health import temperature_status
from .history import History
from .models import Drive, Status
from .smartctl import SmartctlError

# Exit codes follow the Nagios plugin convention so `odh check` can be dropped
# straight into existing monitoring.
EXIT_CODES = {Status.GOOD: 0, Status.CAUTION: 1, Status.BAD: 2, Status.UNKNOWN: 3}

SUBCOMMANDS = ("list", "show", "check", "monitor", "history", "dump")

_COLORS = {Status.GOOD: "32", Status.CAUTION: "33", Status.BAD: "31", Status.UNKNOWN: "36"}
_use_color = False


def color(text: str, status: Status) -> str:
    if not _use_color:
        return text
    return f"\033[{_COLORS[status]}m{text}\033[0m"


def bold(text: str) -> str:
    return f"\033[1m{text}\033[0m" if _use_color else text


def _status_cell(d: Drive) -> str:
    a = d.assessment
    if a is None:
        return "-"
    pct = f"{a.health}%" if a.health is not None else "--"
    return color(f"{a.status.value:<8}{pct:>5}", a.status)


def _table(rows: List[List[str]], headers: List[str]) -> str:
    import re
    strip = lambda s: re.sub(r"\033\[[0-9;]*m", "", s)
    widths = [max(len(strip(str(r[i]))) for r in rows + [headers]) for i in range(len(headers))]
    def line(r):
        return "  ".join(str(c) + " " * (w - len(strip(str(c)))) for c, w in zip(r, widths)).rstrip()
    out = [bold(line(headers)), line(["-" * w for w in widths])]
    out += [line(r) for r in rows]
    return "\n".join(out)


def cmd_list(drives: List[Drive], args) -> int:
    if args.json:
        print(json.dumps([d.to_dict() for d in drives], indent=2, default=str))
        return 0
    if not drives:
        print("No drives found. Try running as Administrator/root, or use --demo.")
        return EXIT_CODES[Status.UNKNOWN]
    rows = []
    for i, d in enumerate(drives):
        t = d.temperature_c
        rows.append([
            str(i), d.device, d.model or "?", d.kind.value, fmt.size(d.capacity_bytes),
            color(fmt.opt(t, " C"), temperature_status(d)) if t is not None else "-",
            fmt.opt(d.power_on_hours, " h"), _status_cell(d),
        ])
    print(_table(rows, ["#", "Device", "Model", "Type", "Size", "Temp", "Power-on", "Health"]))
    for d in drives:
        for e in d.errors:
            print(color(f"{d.device}: {e}", Status.UNKNOWN))
    return 0


def _print_drive(d: Drive) -> None:
    a = d.assessment
    print(bold(f"{d.model or 'Unknown model'}  [{d.device}]"))
    info = [
        ("Status", _status_cell(d)),
        ("Temperature", color(fmt.opt(d.temperature_c, " C"), temperature_status(d))),
        ("Type", d.kind.value + (f", {d.rotation_rpm} RPM" if d.rotation_rpm else "")),
        ("Capacity", fmt.size(d.capacity_bytes)),
        ("Serial", d.serial or "-"),
        ("Firmware", d.firmware or "-"),
        ("Interface", d.interface or d.protocol or "-"),
        ("Power-on time", fmt.hours(d.power_on_hours)),
        ("Power cycles", fmt.opt(d.power_cycles)),
        ("Host reads", fmt.size(d.host_reads_bytes)),
        ("Host writes", fmt.size(d.host_writes_bytes)),
        ("S.M.A.R.T. self-check",
         {True: "PASSED", False: color("FAILED", Status.BAD), None: "-"}[d.smart_passed]),
    ]
    if a and a.wear_remaining is not None:
        info.insert(3, ("Endurance left", f"{a.wear_remaining}%"))
    for k, v in info:
        print(f"  {k:<22}{v}")
    for e in d.errors:
        print(color(f"  ! {e}", Status.UNKNOWN))
    if a and a.findings:
        print(bold("\nFindings"))
        for f in a.findings:
            pen = f" (-{f.penalty:.0f}%)" if f.penalty >= 0.5 else ""
            tag = "info" if f.severity == Status.GOOD else f.severity.value
            print("  " + color(f"[{tag}]", f.severity) + f" {f.message}{pen}")
    if d.attributes:
        print(bold("\nS.M.A.R.T. attributes"))
        nvme = d.attributes[0].value is None
        rows = []
        for at in d.attributes:
            mark = color("*" if at.status != Status.GOOD else " ", at.status)
            if nvme:
                rows.append([mark, f"{at.id:02X}", at.name, at.raw_string])
            else:
                rows.append([mark, f"{at.id:02X}", at.name, fmt.opt(at.value),
                             fmt.opt(at.worst), fmt.opt(at.threshold), at.raw_string])
        headers = ["", "ID", "Attribute", "Raw"] if nvme else \
            ["", "ID", "Attribute", "Current", "Worst", "Thresh", "Raw"]
        print(_table(rows, headers))


def cmd_show(drives: List[Drive], args) -> int:
    targets = drives if not args.drive else [select(drives, args.drive)]
    if targets == [None]:
        print(f"No drive matching {args.drive!r}", file=sys.stderr)
        return EXIT_CODES[Status.UNKNOWN]
    if args.json:
        out = [d.to_dict() for d in targets]
        print(json.dumps(out[0] if args.drive else out, indent=2, default=str))
        return 0
    for i, d in enumerate(targets):
        if i:
            print("\n" + "=" * 72 + "\n")
        _print_drive(d)
    return 0


def notify(title: str, body: str) -> None:
    """Best-effort desktop notification using whatever the OS ships with."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e",
                            f'display notification {json.dumps(body)} with title {json.dumps(title)}'],
                           check=False, timeout=10)
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", "-u", "critical", title, body], check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def run_check(args) -> int:
    drives = load_drives(demo=args.demo, json_files=args.json_file, wake=args.wake)
    worst = Status.GOOD if drives else Status.UNKNOWN
    with History(args.db) as hist:
        for d in drives:
            if d.assessment.status != Status.UNKNOWN:
                worst = Status.worst(worst, d.assessment.status)
            for alert in hist.record(d):
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"{stamp} " + color(f"[{alert.severity.value}]", alert.severity)
                      + f" {alert.drive}: {alert.message}", flush=True)
                if args.notify:
                    notify(f"Drive {alert.severity.value}: {alert.drive}", alert.message)
    if not args.quiet:
        cmd_list(drives, argparse.Namespace(json=False))
    return EXIT_CODES[worst]


def cmd_monitor(args) -> int:
    print(f"Monitoring every {args.interval}s; history in {History(args.db).path}. Ctrl+C to stop.")
    try:
        while True:
            run_check(args)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_history(args) -> int:
    with History(args.db) as hist:
        keys = hist.drives()
        if not args.drive:
            if not keys:
                print("No history yet. Run `odh check` (e.g. from cron/Task Scheduler).")
            for k in keys:
                last = hist.last(k)
                print(f"{k}  last: {datetime.fromtimestamp(last.ts):%Y-%m-%d %H:%M}  "
                      f"{last.status} {fmt.opt(last.health, '%')}")
            return 0
        key = next((k for k in keys if args.drive in k), None)
        if key is None:
            print(f"No history for {args.drive!r}", file=sys.stderr)
            return EXIT_CODES[Status.UNKNOWN]
        rows = [[f"{datetime.fromtimestamp(s.ts):%Y-%m-%d %H:%M}", s.status,
                 fmt.opt(s.health, "%"), fmt.opt(s.temperature, " C"),
                 fmt.opt(s.reallocated), fmt.opt(s.pending), fmt.opt(s.uncorrectable),
                 fmt.opt(s.media_errors), fmt.size(s.host_writes_bytes)]
                for s in hist.series(key, args.limit)]
        print(bold(key))
        print(_table(rows, ["When", "Status", "Health", "Temp", "Realloc", "Pending",
                            "Uncorr", "Media err", "Written"]))
    return 0


def cmd_dump(args) -> int:
    from . import smartctl
    from .core import demo_files
    if args.demo or args.json_file:
        files = args.json_file or [str(f) for f in demo_files()]
        drives = [smartctl.load_json(f) for f in files]
        d = select(drives, args.drive)
        data = json.loads(Path(files[drives.index(d)]).read_text(encoding="utf-8")) if d else None
    else:
        found = smartctl.scan()
        drives = [Drive(device=dev, device_type=typ) for dev, typ in found]
        d = select(drives, args.drive)
        if d is None:  # allow matching by serial or model
            full = [smartctl.read_drive(dev, typ, wake=args.wake) for dev, typ in found]
            d = select(full, args.drive)
        data = smartctl.raw_json(d.device, d.device_type, wake=args.wake) if d else None
    if data is None:
        print(f"No drive matching {args.drive!r}. See `odh list`.", file=sys.stderr)
        return EXIT_CODES[Status.UNKNOWN]
    if not args.keep_ids:
        data = smartctl.anonymize(data)
    text = json.dumps(data, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"Saved {args.output}. Attach it to your GitHub issue.")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="odh", description="OpenDriveHealth: check hard drive and SSD health (S.M.A.R.T.)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    src = argparse.ArgumentParser(add_help=False)
    src.add_argument("--demo", action="store_true", help="use built-in sample drives")
    src.add_argument("--json-file", action="append", metavar="FILE",
                     help="load a saved `smartctl -a --json` dump instead of scanning")
    src.add_argument("--wake", action="store_true",
                     help="also query drives that are spun down (wakes them up)")
    src.add_argument("--no-color", action="store_true")
    db = argparse.ArgumentParser(add_help=False)
    db.add_argument("--db", help="history database path")

    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("list", parents=[src], help="summary of all drives (default)")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("show", parents=[src], help="full details for one or all drives")
    s.add_argument("drive", nargs="?", help="index, device path or serial")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("check", parents=[src, db],
                       help="record a snapshot and print alerts; exit code 0/1/2 = good/caution/bad")
    s.add_argument("--notify", action="store_true", help="show desktop notifications")
    s.add_argument("-q", "--quiet", action="store_true", help="only print alerts")
    s = sub.add_parser("monitor", parents=[src, db], help="run `check` repeatedly")
    s.add_argument("--interval", type=int, default=600, help="seconds between checks")
    s.add_argument("--notify", action="store_true")
    s.add_argument("-q", "--quiet", action="store_true")
    s = sub.add_parser("dump", parents=[src],
                       help="save raw smartctl data for a bug report (serials removed)")
    s.add_argument("drive", help="index, device path or serial (see `odh list`)")
    s.add_argument("-o", "--output", help="file to write (default: print)")
    s.add_argument("--keep-ids", action="store_true",
                   help="don't remove serial numbers and unique IDs")
    s = sub.add_parser("history", parents=[db, src], help="show recorded trends")
    s.add_argument("drive", nargs="?", help="part of a model/serial")
    s.add_argument("--limit", type=int, default=50)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    global _use_color
    argv = list(sys.argv[1:] if argv is None else argv)
    if not any(a in SUBCOMMANDS or a in ("-h", "--help", "--version") for a in argv):
        argv.insert(0, "list")
    args = build_parser().parse_args(argv)
    _use_color = sys.stdout.isatty() and not args.no_color and not os.environ.get("NO_COLOR")
    if _use_color and sys.platform == "win32":
        os.system("")  # enables ANSI escape processing in the Windows console
    try:
        if args.cmd == "check":
            return run_check(args)
        if args.cmd == "monitor":
            return cmd_monitor(args)
        if args.cmd == "history":
            return cmd_history(args)
        if args.cmd == "dump":
            return cmd_dump(args)
        drives = load_drives(demo=args.demo, json_files=args.json_file, wake=args.wake)
        return {"list": cmd_list, "show": cmd_show}[args.cmd](drives, args)
    except SmartctlError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_CODES[Status.UNKNOWN]


if __name__ == "__main__":
    raise SystemExit(main())
