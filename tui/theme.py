"""GRIDPICK · LATTICE theme -- the spec's colour table, nothing else.

Every colour the console draws comes from here. test_lattice.py greps the
rest of tui/ for hex literals, so an inline colour elsewhere fails the suite.
Rich downsamples truecolor to 256/16 colours on its own when the terminal
cannot do better.
"""
from rich.theme import Theme

THEME = Theme({
    "frame": "#3a4250",
    "title": "bold #e6edf3",
    "dim": "#6e7681",
    "predicted": "#f0a832",   # every oracle-derived number
    "realised": "#39d4d0",    # every executor-derived number
    "ok": "#4ade80",
    "warn": "#f59e0b",
    "danger": "#f43f5e",
    "held": "#9ca3af",
    "cursor": "bold #ffffff",
}, inherit=True)
