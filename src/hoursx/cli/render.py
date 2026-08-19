"""Terminal rendering.

Stdlib only — no rendering dependency for a CLI whose job is mostly tables and
status lines. Colour is emitted only to a real TTY, so piping to a file or
another program yields clean text, and ``NO_COLOR`` is honoured.
"""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "grey": "\033[90m",
}

# Status → colour. Keys match the run state machine exactly.
STATUS_COLOURS = {
    "queued": "grey",
    "running": "cyan",
    "awaiting_approval": "yellow",
    "succeeded": "green",
    "failed": "red",
    "cancelled": "magenta",
    "ready": "green",
    "pending": "yellow",
    "ok": "green",
    "error": "red",
}


def colour_enabled(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("HOURSX_FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


def paint(text: str, style: str, *, stream: Any = None) -> str:
    """Wrap text in an ANSI style when the stream supports it."""
    if not colour_enabled(stream) or style not in _ANSI:
        return text
    return f"{_ANSI[style]}{text}{_ANSI['reset']}"


def status(value: str) -> str:
    return paint(value, STATUS_COLOURS.get(value, "reset"))


def heading(text: str) -> str:
    return paint(text, "bold")


def dim(text: str) -> str:
    return paint(text, "dim")


def terminal_width(default: int = 100) -> int:
    try:
        return shutil.get_terminal_size((default, 24)).columns
    except OSError:
        return default


def truncate(text: str, width: int) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def table(rows: list[dict[str, Any]], columns: list[str], *, max_width: int | None = None) -> str:
    """Render rows as an aligned table, fitting the terminal width.

    Column widths are computed from content, then the widest column is squeezed
    until the whole table fits — so the informative narrow columns (id, status)
    stay readable and only the prose column truncates.
    """
    if not rows:
        return dim("(none)")
    limit = max_width or terminal_width()

    widths = {
        column: max(len(column), *(len(str(row.get(column, ""))) for row in rows))
        for column in columns
    }
    gap = 2
    while sum(widths.values()) + gap * (len(columns) - 1) > limit and max(widths.values()) > 8:
        widest = max(widths, key=lambda c: widths[c])
        widths[widest] -= 1

    lines = [
        heading("  ".join(column.upper().ljust(widths[column]) for column in columns).rstrip())
    ]
    for row in rows:
        cells = []
        for column in columns:
            text = truncate(str(row.get(column, "")), widths[column])
            # Pad on the plain text, then colour: ANSI escapes have zero display
            # width, so ljust() on a coloured string over-pads the column.
            padded = text.ljust(widths[column])
            cells.append(padded.replace(text, status(text), 1) if column == "status" else padded)
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def key_values(data: dict[str, Any], *, indent: int = 0) -> str:
    """Render a flat mapping as aligned ``key: value`` lines."""
    if not data:
        return dim("(empty)")
    pad = " " * indent
    width = max(len(str(key)) for key in data)
    lines = []
    for key, value in data.items():
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value[:10]) + (
                f" (+{len(value) - 10} more)" if len(value) > 10 else ""
            )
        lines.append(f"{pad}{dim(str(key).ljust(width))}  {value}")
    return "\n".join(lines)


def bytes_human(count: int | float | None) -> str:
    if count is None:
        return "?"
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024.0:
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024.0
    return f"{size:.1f}PiB"


def bar(fraction: float, width: int = 24) -> str:
    """A simple usage bar; colour tracks severity so a glance is enough."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    style = "green" if fraction < 0.75 else "yellow" if fraction < 0.9 else "red"
    return paint("█" * filled, style) + dim("░" * (width - filled))
