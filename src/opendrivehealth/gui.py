"""Desktop GUI built on Tkinter (ships with Python, so no extra dependencies).

A dark, card-based dashboard: sidebar navigation on the left, a drive picker
along the top, and ring gauges for health, temperature and endurance. The
drive data, findings and history come from the same engine as the CLI.
"""

from __future__ import annotations

import argparse
import json
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import Dict, List, Optional

from . import __version__, fmt
from .cli import notify
from .core import load_drives
from .health import TEMP_LIMITS, temperature_status
from .history import History
from .models import Drive, Status
from .smartctl import SmartctlError
from .widgets import (ACCENT, BG, CARD, CARD_HI, MUTED, SIDEBAR, STATUS_COLOR, TEXT, TRACK,
                      Card, Chart, FlatButton, Ring, Segmented, Toggle, font, init_theme,
                      label, mix)

REFRESH_CHOICES = [("Off", 0), ("1 min", 60), ("5 min", 300), ("10 min", 600),
                   ("30 min", 1800), ("1 hour", 3600)]
TEMP_WORDS = {Status.GOOD: "Normal", Status.CAUTION: "Warm", Status.BAD: "Hot",
              Status.UNKNOWN: "Not reported"}
NAV = [("dashboard", "Drive Dashboard"), ("smart", "S.M.A.R.T. Details"),
       ("history", "Health History"), ("settings", "Settings")]
ROW_TINT = {Status.CAUTION: mix(STATUS_COLOR[Status.CAUTION], CARD, 0.18),
            Status.BAD: mix(STATUS_COLOR[Status.BAD], CARD, 0.2)}

HEALTH_HELP = (
    "Status follows the same rules as CrystalDiskInfo. Bad: the drive reports "
    "FAILING, or an attribute is at or below its threshold. Caution: any "
    "reallocated, pending or uncorrectable sectors.\n\n"
    "The health percentage starts at 100% and loses points for each sign of "
    "wear, with the first problems weighing most. SSDs are also scaled by the "
    "rated endurance left. Every deduction is listed under Diagnostics.\n\n"
    "Watch for changes: a count that keeps growing is the best early warning "
    "of failure, and OpenDriveHealth alerts you on every increase."
)


class App(tk.Tk):
    def __init__(self, args: argparse.Namespace):
        super().__init__()
        init_theme(self)
        self.args = args
        self.title("OpenDriveHealth" + (" (demo)" if args.demo else ""))
        self.geometry("1180x800")
        self.minsize(1000, 700)
        self.drives: List[Drive] = []
        self.selected = 0
        self.results: "queue.Queue" = queue.Queue()
        self.loading = False
        self.refresh_secs = tk.IntVar(value=args.interval)
        self.wake = tk.BooleanVar(value=args.wake)
        # Sample drives should not end up in the user's real history.
        self.record = tk.BooleanVar(value=not args.demo)
        self.next_refresh: Optional[str] = None
        self.current_page = ""
        self.pages: Dict[str, tk.Frame] = {}
        self.nav_items: Dict[str, tuple] = {}

        self._build_sidebar()
        self._build_main()
        self.navigate("dashboard")
        self.bind("<F5>", lambda e: self.refresh())
        self.after(100, self.refresh)
        self.after(200, self._poll)

    # ------------------------------------------------------------ layout --
    def _build_sidebar(self) -> None:
        side = tk.Frame(self, bg=SIDEBAR, width=230)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        brand = tk.Frame(side, bg=SIDEBAR)
        brand.pack(fill="x", padx=20, pady=(24, 28))
        logo = tk.Canvas(brand, width=30, height=30, bg=SIDEBAR, highlightthickness=0)
        logo.create_oval(3, 3, 27, 27, outline=ACCENT, width=4)
        logo.create_oval(12, 12, 18, 18, fill=ACCENT, outline="")
        logo.pack(side="left")
        names = tk.Frame(brand, bg=SIDEBAR)
        names.pack(side="left", padx=10)
        label(names, "OpenDriveHealth", 12, "bold", bg=SIDEBAR).pack(anchor="w")
        label(names, "Disk health monitor", 8, fg=MUTED, bg=SIDEBAR).pack(anchor="w")

        for key, text in NAV:
            row = tk.Frame(side, bg=SIDEBAR, cursor="hand2")
            row.pack(fill="x", pady=1)
            bar = tk.Frame(row, bg=SIDEBAR, width=4)
            bar.pack(side="left", fill="y")
            lbl = label(row, text, 10, "bold", fg=MUTED, bg=SIDEBAR, anchor="w",
                        padx=18, pady=11)
            lbl.pack(side="left", fill="x", expand=True)
            for w in (row, lbl):
                w.bind("<Button-1>", lambda e, k=key: self.navigate(k))
                w.bind("<Enter>", lambda e, k=key: self._paint_nav(k, True))
                w.bind("<Leave>", lambda e, k=key: self._paint_nav(k, False))
            self.nav_items[key] = (row, bar, lbl)

        foot = tk.Frame(side, bg=SIDEBAR)
        foot.pack(side="bottom", fill="x", padx=20, pady=20)
        FlatButton(foot, "Export report", self.save_report, bg=TRACK, hover=CARD_HI,
                   size=9).pack(fill="x")
        label(foot, f"Version {__version__}  ·  MIT licence", 8, fg=MUTED,
              bg=SIDEBAR).pack(anchor="w", pady=(10, 0))

    def _build_main(self) -> None:
        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        self.picker = tk.Frame(main, bg=BG)
        self.picker.pack(fill="x", padx=28, pady=(22, 0))
        self.picker.bind("<Configure>", lambda e: self._flow_picker())

        head = tk.Frame(main, bg=BG)
        head.pack(fill="x", padx=28, pady=(16, 12))
        right = tk.Frame(head, bg=BG)
        right.pack(side="right", anchor="s")
        self.last_checked = label(right, "", 9, fg=MUTED, bg=BG)
        self.last_checked.pack(side="left", padx=(0, 12))
        FlatButton(right, "↻  Refresh", self.refresh).pack(side="left")
        self.title_lbl = label(head, "Looking for drives...", 20, "bold", bg=BG)
        self.title_lbl.pack(anchor="w")
        self.subtitle_lbl = label(head, "", 10, fg=MUTED, bg=BG)
        self.subtitle_lbl.pack(anchor="w")

        container = tk.Frame(main, bg=BG)
        container.pack(fill="both", expand=True, padx=20, pady=(0, 20))
        for key, _ in NAV:
            self.pages[key] = tk.Frame(container, bg=BG)
        self._build_dashboard(self.pages["dashboard"])
        self._build_smart(self.pages["smart"])
        self._build_history(self.pages["history"])
        self._build_settings(self.pages["settings"])

    def _build_dashboard(self, page: tk.Frame) -> None:
        for c in range(3):
            page.columnconfigure(c, weight=1, uniform="col")
        page.rowconfigure(2, weight=1)
        g = dict(padx=8, pady=8, sticky="nsew")

        card = Card(page, "Drive health", height=220)
        card.grid(row=0, column=0, **g)
        self.health_ring = Ring(card.body, 150)
        self.health_ring.pack(expand=True)

        card = Card(page, "Temperature", height=220)
        card.grid(row=0, column=1, **g)
        self.temp_ring = Ring(card.body, 150)
        self.temp_ring.pack(expand=True)

        card = Card(page, "Lifespan", height=220)
        card.grid(row=0, column=2, **g)
        self.life_title = card.title
        self.life_ring = Ring(card.body, 150)
        self.life_text = tk.Frame(card.body, bg=CARD)
        self.life_big = label(self.life_text, "", 26, "bold")
        self.life_big.pack(anchor="w", pady=(16, 0))
        self.life_small = label(self.life_text, "", 10, fg=MUTED, justify="left")
        self.life_small.pack(anchor="w", pady=(6, 0))

        card = Card(page, "Total bytes written", height=180)
        card.grid(row=1, column=0, **g)
        self.tbw_big = label(card.body, "-", 26, "bold")
        self.tbw_big.pack(anchor="w", pady=(16, 0))
        self.tbw_small = label(card.body, "", 10, fg=MUTED)
        self.tbw_small.pack(anchor="w", pady=(6, 0))

        card = Card(page, "Drive information", height=180)
        card.grid(row=1, column=1, columnspan=2, **g)
        grid = tk.Frame(card.body, bg=CARD)
        grid.pack(fill="both", expand=True, pady=(12, 0))
        self.info: Dict[str, tk.Label] = {}
        fields = ["Serial number", "Firmware", "Interface", "Drive type",
                  "Power-on time", "Power cycles", "S.M.A.R.T.", "Device"]
        for i, name in enumerate(fields):
            r, c = i % 4, (i // 4) * 2
            label(grid, name, 9, fg=MUTED, anchor="w").grid(row=r, column=c, sticky="w",
                                                             pady=4, padx=(0, 12))
            v = label(grid, "-", 10, "bold", anchor="w")
            v.grid(row=r, column=c + 1, sticky="w", pady=4, padx=(0, 24))
            self.info[name] = v
        grid.columnconfigure(1, weight=1)
        grid.columnconfigure(3, weight=1)

        card = Card(page, "Diagnostics", height=160)
        card.grid(row=2, column=0, columnspan=3, **g)
        self.diag = tk.Text(card.body, bg=CARD, fg=TEXT, relief="flat", bd=0, wrap="word",
                            highlightthickness=0, font=font(10), padx=0, pady=8,
                            cursor="arrow", spacing3=6)
        sb = ttk.Scrollbar(card.body, orient="vertical", command=self.diag.yview,
                           style="Dark.Vertical.TScrollbar")
        self.diag.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.diag.pack(side="left", fill="both", expand=True)
        for st, color in STATUS_COLOR.items():
            self.diag.tag_configure(st.value, foreground=color, font=font(10, "bold"))
        self.diag.tag_configure("muted", foreground=MUTED)

    def _build_smart(self, page: tk.Frame) -> None:
        card = Card(page, "S.M.A.R.T. attributes", height=400, pad=16)
        card.pack(fill="both", expand=True, padx=8, pady=8)
        wrap = tk.Frame(card.body, bg=CARD)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("id", "name", "value", "worst", "thresh", "raw")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", style="Dark.Treeview")
        for c, text, width, anchor in [
            ("id", "ID", 60, "center"), ("name", "ATTRIBUTE", 300, "w"),
            ("value", "CURRENT", 90, "center"), ("worst", "WORST", 90, "center"),
            ("thresh", "THRESHOLD", 100, "center"), ("raw", "RAW VALUE", 200, "e"),
        ]:
            self.tree.heading(c, text=text, anchor=anchor)
            self.tree.column(c, width=width, anchor=anchor, stretch=(c in ("name", "raw")))
        for st, bg in ROW_TINT.items():
            self.tree.tag_configure(st.value, background=bg)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview,
                           style="Dark.Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

    def _build_history(self, page: tk.Frame) -> None:
        card = Card(page, "Health (%)", height=260)
        card.pack(fill="both", expand=True, padx=8, pady=8)
        self.chart_health = Chart(card.body, ACCENT, y_range=(0, 100), unit="%")
        self.chart_health.pack(fill="both", expand=True, pady=(10, 0))
        card = Card(page, "Temperature (°C)", height=260)
        card.pack(fill="both", expand=True, padx=8, pady=8)
        self.chart_temp = Chart(card.body, STATUS_COLOR[Status.BAD], unit="°")
        self.chart_temp.pack(fill="both", expand=True, pady=(10, 0))

    def _setting_row(self, parent, title: str, desc: str, control) -> None:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", pady=(16, 0))
        text = tk.Frame(row, bg=CARD)
        text.pack(side="left", fill="x", expand=True)
        label(text, title, 11, "bold", anchor="w").pack(anchor="w")
        label(text, desc, 9, fg=MUTED, anchor="w", justify="left",
              wraplength=520).pack(anchor="w", pady=(2, 0))
        control(row).pack(side="right", padx=(20, 0))

    def _build_settings(self, page: tk.Frame) -> None:
        card = Card(page, "Monitoring", height=280)
        card.pack(fill="x", padx=8, pady=8)
        self._setting_row(card.body, "Auto refresh",
                          "How often drives are re-checked in the background.",
                          lambda p: Segmented(p, REFRESH_CHOICES, self.refresh_secs,
                                              self._schedule))
        self._setting_row(card.body, "Record history and alert on changes",
                          "Saves a snapshot each check and warns you when bad sectors, "
                          "status or temperature get worse.",
                          lambda p: Toggle(p, self.record))
        self._setting_row(card.body, "Wake sleeping drives",
                          "Off by default, so checking doesn't spin up idle disks.",
                          lambda p: Toggle(p, self.wake))
        card = Card(page, "How is health calculated?", height=270)
        card.pack(fill="both", expand=True, padx=8, pady=8)
        msg = label(card.body, HEALTH_HELP, 10, fg=MUTED, anchor="nw", justify="left")
        msg.pack(fill="both", expand=True, pady=(12, 0))
        msg.bind("<Configure>", lambda e: msg.config(wraplength=max(200, e.width - 10)))

    # -------------------------------------------------------- navigation --
    def navigate(self, key: str) -> None:
        self.current_page = key
        for k, page in self.pages.items():
            if k == key:
                page.pack(fill="both", expand=True)
            else:
                page.pack_forget()
        for k in self.nav_items:
            self._paint_nav(k, False)

    def _paint_nav(self, key: str, hover: bool) -> None:
        row, bar, lbl = self.nav_items[key]
        active = key == self.current_page
        bg = "#1c1f26" if active else ("#15171b" if hover else SIDEBAR)
        row.config(bg=bg)
        lbl.config(bg=bg, fg=TEXT if active or hover else MUTED)
        bar.config(bg=ACCENT if active else bg)

    # -------------------------------------------------------------- data --
    def refresh(self) -> None:
        if self.loading:
            return
        self.loading = True
        self.last_checked.config(text="Reading drives...")
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
            self.last_checked.config(text="Error reading drives")
            messagebox.showerror("OpenDriveHealth", payload)
        else:
            self.drives = payload
            self.selected = min(self.selected, max(0, len(self.drives) - 1))
            self._render_picker()
            self.show(self.selected)
            self.last_checked.config(text=f"Last checked {datetime.now():%H:%M:%S}")
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
    def _render_picker(self) -> None:
        for w in self.picker.winfo_children():
            w.destroy()
        for i, d in enumerate(self.drives):
            st = d.assessment.status if d.assessment else Status.UNKNOWN
            on = i == self.selected
            bg = ACCENT if on else CARD
            pill = tk.Frame(self.picker, bg=bg, cursor="hand2", padx=12, pady=7)
            dot = tk.Canvas(pill, width=10, height=10, bg=bg, highlightthickness=0)
            dot_color = "#ffffff" if on and st == Status.GOOD else STATUS_COLOR[st]
            dot.create_oval(1, 1, 9, 9, fill=dot_color, outline="")
            dot.pack(side="left", padx=(0, 8))
            name = d.model or d.device
            name = name if len(name) <= 24 else name[:23] + "…"
            txt = label(pill, f"{name}  {fmt.size(d.capacity_bytes)}", 9, "bold",
                        fg="#ffffff" if on else MUTED, bg=bg)
            txt.pack(side="left")
            for w in (pill, dot, txt):
                w.bind("<Button-1>", lambda e, i=i: self.show(i))
        self.after_idle(self._flow_picker)  # pills have no size until Tk measures them

    def _flow_picker(self) -> None:
        """Lay the drive pills out left to right, wrapping onto new rows."""
        self.picker.update_idletasks()
        width = max(self.picker.winfo_width(), 200)
        x = y = row_h = 0
        for pill in self.picker.winfo_children():
            w, h = pill.winfo_reqwidth(), pill.winfo_reqheight()
            if x and x + w > width:
                x, y, row_h = 0, y + row_h + 8, 0
            pill.place(x=x, y=y)
            x += w + 8
            row_h = max(row_h, h)
        self.picker.configure(height=max(1, y + row_h))

    def show(self, index: int) -> None:
        if not self.drives:
            self.title_lbl.config(text="No drives found")
            self.subtitle_lbl.config(text="Run as Administrator / root, or start with --demo")
            return
        if index != self.selected:
            self.selected = index
            self._render_picker()
        d = self.drives[index]
        a = d.assessment
        st = a.status if a else Status.UNKNOWN

        self.title_lbl.config(text=d.model or "Unknown drive")
        sub = [fmt.size(d.capacity_bytes), d.kind.value]
        if d.rotation_rpm:
            sub.append(f"{d.rotation_rpm} RPM")
        sub.append(d.device)
        self.subtitle_lbl.config(text="  ·  ".join(sub))

        if a and a.health is not None:
            self.health_ring.set(a.health / 100, f"{a.health}%", st.value, STATUS_COLOR[st])
        else:
            self.health_ring.set(None, "--", st.value, STATUS_COLOR[st])

        tst = temperature_status(d)
        if d.temperature_c is not None:
            top = TEMP_LIMITS[d.kind][1] + 10
            self.temp_ring.set(d.temperature_c / top, f"{d.temperature_c}°C",
                               TEMP_WORDS[tst], STATUS_COLOR[tst])
        else:
            self.temp_ring.set(None, "--", TEMP_WORDS[tst], STATUS_COLOR[tst])

        self._render_lifespan(d)

        self.tbw_big.config(text=fmt.size(d.host_writes_bytes))
        if d.host_writes_bytes is None:
            self.tbw_small.config(text="Not reported by this drive")
        else:
            self.tbw_small.config(text="Total read not reported" if d.host_reads_bytes is None
                                  else f"Total read  {fmt.size(d.host_reads_bytes)}")

        i = self.info
        i["Serial number"].config(text=d.serial or "-")
        i["Firmware"].config(text=d.firmware or "-")
        i["Interface"].config(text=d.interface or d.protocol or "-")
        i["Drive type"].config(text=d.kind.value + (f", {d.rotation_rpm} RPM"
                                                    if d.rotation_rpm else ""))
        i["Power-on time"].config(text="-" if d.power_on_hours is None
                                  else f"{d.power_on_hours:,} h")
        i["Power cycles"].config(text=fmt.opt(d.power_cycles))
        text, sst = {True: ("Passed", Status.GOOD), False: ("FAILED", Status.BAD),
                     None: ("-", Status.UNKNOWN)}[d.smart_passed]
        i["S.M.A.R.T."].config(text=text, fg=TEXT if sst == Status.UNKNOWN else STATUS_COLOR[sst])
        i["Device"].config(text=f"{d.device} ({d.device_type or d.protocol})")

        self._render_diagnostics(d)
        self._render_attributes(d)
        self._render_history(d)

    def _render_lifespan(self, d: Drive) -> None:
        a = d.assessment
        if a and a.wear_remaining is not None:
            self.life_title.config(text="ENDURANCE LEFT")
            self.life_text.pack_forget()
            self.life_ring.pack(expand=True)
            wst = Status.CAUTION if a.wear_remaining <= 10 else Status.GOOD
            self.life_ring.set(a.wear_remaining / 100, f"{a.wear_remaining}%",
                               "Rated life", STATUS_COLOR[wst])
            return
        self.life_title.config(text="POWER-ON TIME")
        self.life_ring.pack_forget()
        self.life_text.pack(fill="both", expand=True)
        h = d.power_on_hours
        if h is None:
            self.life_big.config(text="-")
            self.life_small.config(text="")
            return
        years, days = divmod(h // 24, 365)
        self.life_big.config(text=f"{years}y {days}d" if years else f"{days}d {h % 24}h")
        self.life_small.config(text=f"{h:,} hours\n{fmt.opt(d.power_cycles)} power cycles")

    def _render_diagnostics(self, d: Drive) -> None:
        t = self.diag
        t.config(state="normal")
        t.delete("1.0", "end")
        for e in d.errors:
            t.insert("end", "●  ", Status.UNKNOWN.value)
            t.insert("end", e + "\n")
        items = d.assessment.findings if d.assessment else []
        if not items and not d.errors:
            t.insert("end", "●  ", Status.GOOD.value)
            t.insert("end", "No problems found. This drive shows no signs of degradation.\n")
        for f in items:
            t.insert("end", "●  ", f.severity.value)
            t.insert("end", f.message)
            if f.penalty >= 0.5:
                t.insert("end", f"   -{f.penalty:.0f}%", "muted")
            t.insert("end", "\n")
        t.config(state="disabled")

    def _render_attributes(self, d: Drive) -> None:
        self.tree.delete(*self.tree.get_children())
        for at in d.attributes:
            tags = (at.status.value,) if at.status in ROW_TINT else ()
            self.tree.insert("", "end", tags=tags, values=(
                f"{at.id:02X}", at.name, fmt.opt(at.value), fmt.opt(at.worst),
                fmt.opt(at.threshold), at.raw_string))
        if d.attributes and d.attributes[0].value is None:  # NVMe: values only
            self.tree.configure(displaycolumns=("id", "name", "raw"))
            self.tree.heading("raw", text="VALUE")
        else:
            self.tree.configure(displaycolumns=("id", "name", "value", "worst", "thresh", "raw"))
            self.tree.heading("raw", text="RAW VALUE")

    def _render_history(self, d: Drive) -> None:
        try:
            with History(self.args.db) as hist:
                series = hist.series(d.key, 2000)
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
        self.last_checked.config(text=f"Saved {path}")


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
