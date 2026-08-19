"""HoursX desktop application.

Tkinter, deliberately: it ships with CPython, so ``hoursx gui`` works on a
freshly installed machine with no extra packages, no browser, and no server —
which is exactly the situation an operator is in when they most need it.

The GUI drives the same in-process engine as the CLI, so it is a second front
end onto one runtime rather than a parallel implementation.
"""

from hoursx.gui.app import launch

__all__ = ["launch"]
