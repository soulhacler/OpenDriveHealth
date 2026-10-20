"""Desktop GUI built on Tkinter (ships with Python, so no extra dependencies).

Layout borrows from CrystalDiskInfo (drive buttons, big health/temperature
boxes, attribute table) and HDD Sentinel (health percentage, plain-English
findings, history charts and background monitoring with alerts).
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional

from . import __version__, fmt
from .cli import notify
from .core import load_drives
from .health import temperature_status
from .history import History, Snapshot
from .models import Drive, DriveKind, Status
from .smartctl import SmartctlError

PALETTE = {
    Status.GOOD: ("#2f80ed", "#ffffff"),  # CrystalDiskInfo uses blue for Good
    Status.CAUTION: ("#f2c94c", "#000000"),
    Status.BAD: ("#eb5757", "#ffffff"),
    Status.UNKNOWN: ("#9e9e9e", "#ffffff"),
}
ROW_TINT = {Status.CAUTION: "#fff4cc", Status.BAD: "#ffd6d6"}
REFRESH_CHOICES = [("Off", 0), ("1 minute", 60), ("5 minutes", 300),
                   ("10 minutes", 600), ("30 minutes", 1800), ("1 hour", 3600)]


class Chart(tk.Canvas):
    """Minimal line chart; avoids pulling in matplotlib."""

    def __init__(self, master, title: str, color: str, y_range=None, **kw):
        super().__init__(master, height=150, background="white",
                         highlightthickness=1, highlightbackground="#ccc", **kw)
        self.title, self.color, self.y_range = title, color, y_range
        self.points: List[tuple] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, points: List[tuple]) -> None:
        self.points = [(x, y) for x, y in points if y is not None]
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        pad_l, pad_r, pad_t, pad_b = 40, 12, 22, 22
        self.create_text(pad_l, 4, anchor="nw", text=self.title, font=("TkDefaultFont", 9, "bold"))
        if len(self.points) < 2:
            self.create_text(w / 2, h / 2, text="Not enough history yet", fill="#888")
            return
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        x0, x1 = min(xs), max(xs) or 1
        y0, y1 = self.y_range or (min(ys), max(ys))
        if y0 == y1:
            y0, y1 = y0 - 1, y1 + 1
        sx = lambda x: pad_l + (x - x0) / ((x1 - x0) or 1) * (w - pad_l - pad_r)
        sy = lambda y: h - pad_b - (y - y0) / (y1 - y0) * (h - pad_t - pad_b)
        for v in (y0, (y0 + y1) / 2, y1):
            self.create_line(pad_l, sy(v), w - pad_r, sy(v), fill="#eee")
            self.create_text(pad_l - 4, sy(v), anchor="e", text=f"{v:.0f}", font=("TkDefaultFont", 8))
        for x, anchor in ((x0, "nw"), (x1, "ne")):
            self.create_text(sx(x), h - pad_b + 4, anchor=anchor, font=("TkDefaultFont", 8),
                             text=datetime.fromtimestamp(x).strftime("%Y-%m-%d %H:%M"))
        coords = [c for x, y in self.points for c in (sx(x), sy(y))]
        self.create_line(*coords, fill=self.color, width=2)


class App(tk.Tk):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        self.args = args
        self.title(f"OpenDriveHealth {__version__}" + (" (demo)" if args.demo else ""))
        self.geometry("980x760")
        self.minsize(820, 600)
        self.drives: List[Drive] = []
        self.selected = 0
        self.results: "queue.Queue" = queue.Queue()
        self.loading = False
        self.refresh_secs = tk.IntVar(value=args.interval)
        self.wake = tk.BooleanVar(value=args.wake)
        # Sample drives should not end up in the user's real history.
        self.record = tk.BooleanVar(value=not args.demo)
        self.next_refresh: Optional[str] = None

        style = ttk.Style(self)
        if "clam" in style.theme_names() and sys.platform.startswith("linux"):
            style.theme_use("clam")
        style.configure("Treeview", rowheight=22)

        self._build_menu()
        self._build_layout()
        self.bind("<F5>", lambda e: self.refresh())
        self.after(100, self.refresh)
        self.after(200, self._poll)

    # ------------------------------------------------------------------ UI --
    def _build_menu(self) -> None:
        bar = tk.Menu(self)
        m = tk.Menu(bar, tearoff=False)
        m.add_command(label="Refresh", accelerator="F5", command=self.refresh)
        m.add_command(label="Save report...", command=self.save_report)
        m.add_separator()
        m.add_command(label="Exit", command=self.destroy)
        bar.add_cascade(label="File", menu=m)
        o = tk.Menu(bar, tearoff=False)
        r = tk.Menu(o, tearoff=False)
        for label, secs in REFRESH_CHOICES:
            r.add_radiobutton(label=label, value=secs, variable=self.refresh_secs,
                              command=self._schedule)
        o.add_cascade(label="Auto refresh", menu=r)
        o.add_checkbutton(label="Wake sleeping drives", variable=self.wake)
        o.add_checkbutton(label="Record history and alert on changes", variable=self.record)
        bar.add_cascade(label="Options", menu=o)
        h = tk.Menu(bar, tearoff=False)
        h.add_command(label="How is health calculated?", command=self.explain)
        h.add_command(label="About", command=lambda: messagebox.showinfo(
            "About", f"OpenDriveHealth {__version__}\nFree and open-source (MIT).\n"
                     "Powered by smartmontools."))
        bar.add_cascade(label="Help", menu=h)
        self.config(menu=bar)

    def _build_layout(self) -> None:
        self.drive_bar = ttk.Frame(self, padding=(8, 6))
        self.drive_bar.pack(fill="x")

        self.header = ttk.Label(self, font=("TkDefaultFont", 14, "bold"), anchor="center")
        self.header.pack(fill="x", pady=(0, 6))

        top = ttk.Frame(self, padding=(8, 0))
        top.pack(fill="x")
        left = ttk.Frame(top)
        left.pack(side="left", fill="y", padx=(0, 10))
        ttk.Label(left, text="Health Status").pack(anchor="w")
        self.health_box = tk.Label(left, width=14, height=3, font=("TkDefaultFont", 16, "bold"),
                                   relief="ridge", bd=2)
        self.health_box.pack(pady=(0, 8))
        ttk.Label(left, text="Temperature").pack(anchor="w")
        self.temp_box = tk.Label(left, width=14, height=2, font=("TkDefaultFont", 16, "bold"),
                                 relief="ridge", bd=2)
        self.temp_box.pack()
        ttk.Label(left, text="Endurance left").pack(anchor="w", pady=(8, 0))
        self.wear_bar = ttk.Progressbar(left, length=150, maximum=100)
        self.wear_bar.pack(anchor="w")
        self.wear_lbl = ttk.Label(left, text="-")
        self.wear_lbl.pack(anchor="w")

        self.info = ttk.Frame(top)
        self.info.pack(side="left", fill="both", expand=True)
        self.info_vars = {}
        fields = ["Firmware", "Serial Number", "Interface", "Type", "Capacity",
                  "Rotation Rate", "Power On Hours", "Power On Count", "Host Reads",
                  "Host Writes", "S.M.A.R.T.", "Device"]
        for i, name in enumerate(fields):
            r, c = i % 6, (i // 6) * 2
            ttk.Label(self.info, text=name + ":", anchor="e", width=15).grid(
                row=r, column=c, sticky="e", padx=(0, 4), pady=1)
            v = tk.StringVar(value="-")
            ttk.Entry(self.info, textvariable=v, state="readonly", width=30).grid(
                row=r, column=c + 1, sticky="we", pady=1)
            self.info_vars[name] = v
        self.info.columnconfigure(1, weight=1)
        self.info.columnconfigure(3, weight=1)

        nb = ttk.Notebook(self, padding=8)
        nb.pack(fill="both", expand=True)

        tab = ttk.Frame(nb)
        cols = ("id", "name", "value", "worst", "thresh", "raw")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings")
        for c, label, width, anchor in [
            ("id", "ID", 50, "center"), ("name", "Attribute Name", 260, "w"),
            ("value", "Current", 70, "center"), ("worst", "Worst", 70, "center"),
            ("thresh", "Threshold", 80, "center"), ("raw", "Raw Values", 200, "e"),
        ]:
            self.tree.heading(c, text=label)
            self.tree.column(c, width=width, anchor=anchor, stretch=(c in ("name", "raw")))
        for st, bg in ROW_TINT.items():
            self.tree.tag_configure(st.value, background=bg)
        sb = ttk.Scrollbar(tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        nb.add(tab, text="S.M.A.R.T.")

        tab = ttk.Frame(nb)
        self.findings = tk.Text(tab, wrap="word", height=8, relief="flat", padx=8, pady=8,
                                font=("TkDefaultFont", 10))
        for st, (bg, _) in PALETTE.items():
            self.findings.tag_configure(st.value, foreground=bg if st != Status.CAUTION else "#b8860b",
                                        font=("TkDefaultFont", 10, "bold"))
        self.findings.pack(fill="both", expand=True)
        nb.add(tab, text="Findings")

        tab = ttk.Frame(nb, padding=4)
        self.chart_health = Chart(tab, "Health (%)", "#2f80ed", y_range=(0, 100))
        self.chart_health.pack(fill="both", expand=True, pady=(0, 6))
        self.chart_temp = Chart(tab, "Temperature (C)", "#eb5757")
        self.chart_temp.pack(fill="both", expand=True)
        nb.add(tab, text="History")

        self.status = ttk.Label(self, relief="sunken", anchor="w", padding=(6, 2))
        self.status.pack(fill="x", side="bottom")

    # -------------------------------------------------------------- data --
    def refresh(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.status.config(text="Reading drives...")
        threading.Thread(target=self._worker, daemon=True).start()

    def _worker(self) -> None:
        try:
            drives = load_drives(demo=self.args.demo, json_files=self.args.json_file,
                                 wake=self.wake.get())
            alerts = []
            if self.record.get():
                with History(self.args.db) as hist:
                    for d in drives:
                        alerts += hist.record(d)
            self.results.put(("ok", drives, alerts))
        except SmartctlError as e:
            self.results.put(("error", str(e), []))
        except Exception as e:  # keep the GUI alive whatever happens
            self.results.put(("error", f"{type(e).__name__}: {e}", []))

    def _poll(self) -> None:
        try:
            kind, payload, alerts = self.results.get_nowait()
        except queue.Empty:
            self.after(200, self._poll)
            return
        self.loading = False
        if kind == "error":
            self.status.config(text="Error")
            messagebox.showerror("OpenDriveHealth", payload)
        else:
            self.drives = payload
            self.selected = min(self.selected, max(0, len(self.drives) - 1))
            self._render_drive_bar()
            self.show(self.selected)
            self.status.config(text=f"{len(self.drives)} drive(s) - last checked "
                                    f"{datetime.now():%H:%M:%S}")
            if alerts:
                text = "\n".join(f"[{a.severity.value}] {a.drive}: {a.message}" for a in alerts)
                notify("OpenDriveHealth alert", text)
                messagebox.showwarning("Drive alert", text)
            if not self.drives:
                messagebox.showinfo("OpenDriveHealth",
                    "No drives were found.\n\nS.M.A.R.T. access usually needs "
                    "Administrator (Windows) or root (Linux/macOS) rights.\n"
                    "Start with --demo to try the interface with sample drives.")
        self._schedule()
        self.after(200, self._poll)

    def _schedule(self) -> None:
        if self.next_refresh:
            self.after_cancel(self.next_refresh)
            self.next_refresh = None
        if self.refresh_secs.get() > 0:
            self.next_refresh = self.after(self.refresh_secs.get() * 1000, self.refresh)

    # ------------------------------------------------------------ render --
    def _render_drive_bar(self) -> None:
        for w in self.drive_bar.winfo_children():
            w.destroy()
        for i, d in enumerate(self.drives):
            st = d.assessment.status if d.assessment else Status.UNKNOWN
            bg, fg = PALETTE[st]
            temp = fmt.opt(d.temperature_c, " C")
            label = f"{st.value}\n{temp}\n{d.device.rsplit('/', 1)[-1]}"
            b = tk.Button(self.drive_bar, text=label, bg=bg, fg=fg, activebackground=bg,
                          width=10, relief="sunken" if i == self.selected else "raised",
                          command=lambda i=i: self.show(i))
            b.pack(side="left", padx=2)

    def show(self, index: int) -> None:
        if not self.drives:
            self.header.config(text="No drives")
            return
        self.selected = index
        for i, b in enumerate(self.drive_bar.winfo_children()):
            b.config(relief="sunken" if i == index else "raised")
        d = self.drives[index]
        a = d.assessment
        self.header.config(text=f"{d.model or 'Unknown drive'}  {fmt.size(d.capacity_bytes)}")

        st = a.status if a else Status.UNKNOWN
        bg, fg = PALETTE[st]
        pct = f"\n{a.health} %" if a and a.health is not None else ""
        self.health_box.config(text=f"{st.value}{pct}", bg=bg, fg=fg)
        tst = temperature_status(d)
        bg, fg = PALETTE[tst]
        self.temp_box.config(text=fmt.opt(d.temperature_c, " °C"), bg=bg, fg=fg)
        if a and a.wear_remaining is not None:
            self.wear_bar.config(value=a.wear_remaining)
            self.wear_lbl.config(text=f"{a.wear_remaining} %")
        else:
            self.wear_bar.config(value=0)
            self.wear_lbl.config(text="n/a")

        v = self.info_vars
        v["Firmware"].set(d.firmware or "-")
        v["Serial Number"].set(d.serial or "-")
        v["Interface"].set(d.interface or d.protocol or "-")
        v["Type"].set(d.kind.value)
        v["Capacity"].set(fmt.size(d.capacity_bytes))
        v["Rotation Rate"].set(f"{d.rotation_rpm} RPM" if d.rotation_rpm else
                               "- (SSD)" if d.kind in (DriveKind.SSD, DriveKind.NVME) else "-")
        v["Power On Hours"].set(fmt.hours(d.power_on_hours))
        v["Power On Count"].set(fmt.opt(d.power_cycles))
        v["Host Reads"].set(fmt.size(d.host_reads_bytes))
        v["Host Writes"].set(fmt.size(d.host_writes_bytes))
        v["S.M.A.R.T."].set({True: "Self-assessment PASSED", False: "Self-assessment FAILED",
                             None: "-"}[d.smart_passed])
        v["Device"].set(f"{d.device} ({d.device_type or d.protocol})")

        self.tree.delete(*self.tree.get_children())
        for at in d.attributes:
            tags = (at.status.value,) if at.status in ROW_TINT else ()
            self.tree.insert("", "end", tags=tags, values=(
                f"{at.id:02X}", at.name, fmt.opt(at.value), fmt.opt(at.worst),
                fmt.opt(at.threshold), at.raw_string))
        if d.attributes and d.attributes[0].value is None:  # NVMe: values only
            self.tree.configure(displaycolumns=("id", "name", "raw"))
            self.tree.heading("raw", text="Value")
        else:
            self.tree.configure(displaycolumns=("id", "name", "value", "worst", "thresh", "raw"))
            self.tree.heading("raw", text="Raw Values")

        self.findings.config(state="normal")
        self.findings.delete("1.0", "end")
        for e in d.errors:
            self.findings.insert("end", "[!] ", Status.UNKNOWN.value)
            self.findings.insert("end", e + "\n\n")
        items = a.findings if a else []
        if not items and not d.errors:
            self.findings.insert("end", "No problems found. ", Status.GOOD.value)
            self.findings.insert("end", "This drive shows no signs of degradation.\n")
        for f in items:
            tag = "info" if f.severity == Status.GOOD else f.severity.value
            self.findings.insert("end", f"[{tag}] ", f.severity.value)
            pen = f"  (-{f.penalty:.0f}%)" if f.penalty >= 0.5 else ""
            self.findings.insert("end", f.message + pen + "\n\n")
        self.findings.config(state="disabled")

        self._render_history(d)

    def _render_history(self, d: Drive) -> None:
        try:
            with History(self.args.db) as hist:
                series: List[Snapshot] = hist.series(d.key, 2000)
        except Exception:
            series = []
        self.chart_health.set([(s.ts, s.health) for s in series])
        self.chart_temp.set([(s.ts, s.temperature) for s in series])

    # ----------------------------------------------------------- actions --
    def save_report(self) -> None:
        if not self.drives:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", initialfile=f"drive-report-{datetime.now():%Y%m%d-%H%M}",
            filetypes=[("JSON", "*.json"), ("Text", "*.txt")])
        if not path:
            return
        if path.endswith(".txt"):
            import contextlib
            import io

            from .cli import _print_drive
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                for d in self.drives:
                    _print_drive(d)
                    print("\n" + "=" * 72 + "\n")
            text = buf.getvalue()
        else:
            text = json.dumps([d.to_dict() for d in self.drives], indent=2, default=str)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self.status.config(text=f"Saved {path}")

    def explain(self) -> None:
        messagebox.showinfo("How is health calculated?",
            "Status (Good / Caution / Bad) follows the same rules as CrystalDiskInfo:\n"
            " - Bad: the drive reports FAILING, or an attribute is at/below its threshold\n"
            " - Caution: any reallocated, pending or uncorrectable sectors\n\n"
            "The health percentage starts at 100% and loses points for each sign of "
            "wear, with the first problems weighing most. SSDs are also scaled by the "
            "rated endurance left. Every deduction is listed on the Findings tab.\n\n"
            "Tip: watch for CHANGES. A count that keeps growing is the best early "
            "warning of failure; OpenDriveHealth alerts on every increase.")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="odh-gui", description="OpenDriveHealth desktop app")
    p.add_argument("--demo", action="store_true", help="use built-in sample drives")
    p.add_argument("--json-file", action="append", metavar="FILE")
    p.add_argument("--wake", action="store_true", help="query drives that are spun down")
    p.add_argument("--interval", type=int, default=600, help="auto refresh seconds (0 = off)")
    p.add_argument("--db", help="history database path")
    args = p.parse_args(argv)
    App(args).mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
