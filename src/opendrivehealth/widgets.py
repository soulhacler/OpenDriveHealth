"""Dark theme and custom Tkinter widgets (cards, ring gauges, toggles, charts).

Everything is drawn on plain Tk canvases so the app looks the same on
Windows, macOS and Linux without any third-party UI toolkit.
"""

from __future__ import annotations

import tkinter as tk
from datetime import datetime
from tkinter import font as tkfont
from tkinter import ttk
from typing import Callable, List, Optional, Sequence, Tuple

from .models import Status

BG = "#16181d"  # page background
SIDEBAR = "#0e0f12"
CARD = "#21242b"
CARD_HI = "#2b2f38"
TRACK = "#353a44"
TEXT = "#eceef1"
MUTED = "#969ca6"
ACCENT = "#3d7eff"

STATUS_COLOR = {
    Status.GOOD: "#3d7eff",
    Status.CAUTION: "#f5b83d",
    Status.BAD: "#ff5c5c",
    Status.UNKNOWN: "#8a8f98",
}

FAMILY = "TkDefaultFont"
_PREFERRED = ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Ubuntu",
              "Cantarell", "Noto Sans", "DejaVu Sans")


def init_theme(root: tk.Tk) -> None:
    """Pick a modern system font and style the few ttk widgets we use."""
    global FAMILY
    available = set(tkfont.families(root))
    FAMILY = next((f for f in _PREFERRED if f in available), "TkDefaultFont")
    root.configure(bg=BG)
    root.option_add("*Font", font(10))

    style = ttk.Style(root)
    style.theme_use("clam")  # the only built-in theme that honours custom colours
    style.configure("Dark.Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT,
                    bordercolor=CARD, borderwidth=0, rowheight=28, font=font(10))
    style.map("Dark.Treeview", background=[("selected", CARD_HI)],
              foreground=[("selected", TEXT)])
    style.configure("Dark.Treeview.Heading", background=CARD, foreground=MUTED,
                    bordercolor=CARD, relief="flat", font=font(9, "bold"), padding=(6, 6))
    style.map("Dark.Treeview.Heading", background=[("active", CARD_HI)])
    style.layout("Dark.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Dark.Vertical.TScrollbar", troughcolor=CARD, background=TRACK,
                    bordercolor=CARD, arrowcolor=MUTED, lightcolor=TRACK, darkcolor=TRACK,
                    gripcount=0)
    style.map("Dark.Vertical.TScrollbar", background=[("active", MUTED)])


def mix(c1: str, c2: str, t: float) -> str:
    """Blend two #rrggbb colours (t = share of c1). Used instead of stipple,
    which Windows and macOS ignore."""
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x * t + y * (1 - t)):02x}" for x, y in zip(a, b))


def font(size: int, weight: str = "normal") -> Tuple[str, int, str]:
    return (FAMILY, size, weight)


def rounded_rect(c: tk.Canvas, x1, y1, x2, y2, r, **kw) -> int:
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


def label(master, text="", size=10, weight="normal", fg=TEXT, bg=CARD, **kw) -> tk.Label:
    return tk.Label(master, text=text, font=font(size, weight), fg=fg, bg=bg, **kw)


class Card(tk.Canvas):
    """Rounded panel. Put children in ``card.body``."""

    def __init__(self, master, title: str = "", height: int = 200, pad: int = 18,
                 bg: str = BG, fill: str = CARD, **kw):
        super().__init__(master, height=height, bg=bg, highlightthickness=0, bd=0, **kw)
        self.pad, self.fill = pad, fill
        self.body = tk.Frame(self, bg=fill)
        self.title: Optional[tk.Label] = None
        if title:
            self.title = label(self.body, title.upper(), 9, "bold", fg=MUTED, bg=fill)
            self.title.pack(anchor="w")
        self._win = self.create_window(pad, pad, anchor="nw", window=self.body)
        self.bind("<Configure>", self._layout)

    def _layout(self, _e=None) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        self.delete("bg")
        rounded_rect(self, 1, 1, w - 1, h - 1, 16, fill=self.fill, outline="", tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(1, w - 2 * self.pad),
                           height=max(1, h - 2 * self.pad))


class Ring(tk.Canvas):
    """Circular gauge with a big value in the middle."""

    def __init__(self, master, size: int = 150, thickness: int = 12, bg: str = CARD):
        super().__init__(master, width=size, height=size, bg=bg, highlightthickness=0)
        self.size, self.thickness = size, thickness

    def set(self, fraction: Optional[float], text: str, sub: str = "",
            color: str = ACCENT) -> None:
        self.delete("all")
        s, t = self.size, self.thickness
        box = (t, t, s - t, s - t)
        self.create_arc(*box, start=225, extent=-270, style="arc", outline=TRACK, width=t)
        if fraction is not None and fraction > 0:
            ext = -270 * max(0.0, min(1.0, fraction))
            self.create_arc(*box, start=225, extent=ext, style="arc", outline=color, width=t)
        self.create_text(s / 2, s / 2 - (8 if sub else 0), text=text, fill=TEXT,
                         font=font(int(s / 6), "bold"))
        if sub:
            self.create_text(s / 2, s / 2 + s / 7, text=sub, fill=color, font=font(10, "bold"))


class Toggle(tk.Canvas):
    """iOS-style switch bound to a BooleanVar."""

    def __init__(self, master, variable: tk.BooleanVar, command: Optional[Callable] = None,
                 bg: str = CARD):
        super().__init__(master, width=46, height=26, bg=bg, highlightthickness=0,
                         cursor="hand2")
        self.var, self.command = variable, command
        self.bind("<Button-1>", self._click)
        variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def _click(self, _e) -> None:
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self) -> None:
        self.delete("all")
        on = self.var.get()
        rounded_rect(self, 1, 1, 45, 25, 12, fill=ACCENT if on else TRACK, outline="")
        x = 32 if on else 14
        self.create_oval(x - 9, 4, x + 9, 22, fill="#ffffff", outline="")


class FlatButton(tk.Label):
    def __init__(self, master, text: str, command: Callable, bg: str = ACCENT,
                 fg: str = "#ffffff", hover: str = "#5a92ff", size: int = 10, **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, font=font(size, "bold"),
                         padx=16, pady=7, cursor="hand2", **kw)
        self._bg, self._hover = bg, hover
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self.config(bg=self._hover))
        self.bind("<Leave>", lambda e: self.config(bg=self._bg))


class Segmented(tk.Frame):
    """Row of mutually exclusive pills bound to an IntVar."""

    def __init__(self, master, options: Sequence[Tuple[str, int]], variable: tk.IntVar,
                 command: Optional[Callable] = None, bg: str = CARD):
        super().__init__(master, bg=bg)
        self.var, self.command, self.items = variable, command, []
        for text, value in options:
            lbl = tk.Label(self, text=text, font=font(9, "bold"), padx=12, pady=6,
                           cursor="hand2")
            lbl.pack(side="left", padx=(0, 4))
            lbl.bind("<Button-1>", lambda e, v=value: self._pick(v))
            self.items.append((lbl, value))
        self._draw()

    def _pick(self, value: int) -> None:
        self.var.set(value)
        self._draw()
        if self.command:
            self.command()

    def _draw(self) -> None:
        for lbl, value in self.items:
            on = value == self.var.get()
            lbl.config(bg=ACCENT if on else TRACK, fg="#ffffff" if on else MUTED)


class Chart(tk.Canvas):
    """Minimal dark line chart with a soft area fill."""

    def __init__(self, master, color: str, y_range=None, unit: str = "", bg: str = CARD):
        super().__init__(master, height=170, bg=bg, highlightthickness=0)
        self.color, self.y_range, self.unit = color, y_range, unit
        self.points: List[tuple] = []
        self.bind("<Configure>", lambda e: self.redraw())

    def set(self, points: List[tuple]) -> None:
        self.points = [(x, y) for x, y in points if y is not None]
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        pl, pr, pt, pb = 44, 8, 8, 24
        if len(self.points) < 2:
            self.create_text(w / 2, h / 2, text="Not enough history yet - it builds up "
                             "with each check.", fill=MUTED, font=font(10))
            return
        xs, ys = [p[0] for p in self.points], [p[1] for p in self.points]
        x0, x1 = min(xs), max(xs)
        y0, y1 = self.y_range or (min(ys) - 2, max(ys) + 2)
        sx = lambda x: pl + (x - x0) / ((x1 - x0) or 1) * (w - pl - pr)
        sy = lambda y: h - pb - (y - y0) / ((y1 - y0) or 1) * (h - pt - pb)
        for v in (y0, (y0 + y1) / 2, y1):
            self.create_line(pl, sy(v), w - pr, sy(v), fill=TRACK, dash=(2, 4))
            self.create_text(pl - 8, sy(v), anchor="e", text=f"{v:.0f}{self.unit}",
                             fill=MUTED, font=font(8))
        for x, anchor in ((x0, "nw"), (x1, "ne")):
            self.create_text(sx(x), h - pb + 6, anchor=anchor, fill=MUTED, font=font(8),
                             text=datetime.fromtimestamp(x).strftime("%d %b %Y"))
        line = [c for x, y in self.points for c in (sx(x), sy(y))]
        area = [sx(x0), h - pb, *line, sx(x1), h - pb]
        self.create_polygon(area, fill=mix(self.color, CARD, 0.2), outline="")
        self.create_line(*line, fill=self.color, width=2.5)
        lx, ly = sx(xs[-1]), sy(ys[-1])
        self.create_oval(lx - 4, ly - 4, lx + 4, ly + 4, fill=self.color, outline=CARD, width=2)
