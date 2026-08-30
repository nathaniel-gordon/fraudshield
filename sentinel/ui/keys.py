"""Single-keystroke reading, without curses.

Two implementations because the platforms genuinely differ: ``msvcrt`` on Windows,
raw-mode ``termios`` elsewhere. Both normalise arrow keys and control characters into
the same small vocabulary so the dashboard's key handling has no platform branches.
"""
from __future__ import annotations

import sys

#: Normalised key names the dashboard understands.
UP, DOWN, PAGE_UP, PAGE_DOWN, HOME, END, ENTER, ESC = (
    "UP",
    "DOWN",
    "PGUP",
    "PGDN",
    "HOME",
    "END",
    "ENTER",
    "ESC",
)

_WIN_SPECIAL = {
    "H": UP,
    "P": DOWN,
    "I": PAGE_UP,
    "Q": PAGE_DOWN,
    "G": HOME,
    "O": END,
}

_VT_SPECIAL = {
    "A": UP,
    "B": DOWN,
    "5~": PAGE_UP,
    "6~": PAGE_DOWN,
    "H": HOME,
    "F": END,
}


def read_key() -> str:
    """Block until one key is pressed and return its normalised name.

    Returns ``"q"`` at end of input so a dashboard driven from a closed stdin exits
    instead of spinning — a daemon-like UI must never hang.
    """
    if sys.platform == "win32":
        return _read_key_windows()
    return _read_key_posix()


def _read_key_windows() -> str:  # pragma: no cover - needs a real console
    import msvcrt

    ch = msvcrt.getwch()
    if not ch or ch == "\x04":
        # A console whose input has gone away returns nothing. Spinning on that
        # would peg a core; quitting is the only sane reading of "no more keys".
        return "q"
    if ch in ("\x00", "\xe0"):
        special = msvcrt.getwch()
        return _WIN_SPECIAL.get(special, "") if special else "q"
    if ch in ("\r", "\n"):
        return ENTER
    if ch == "\x1b":
        return ESC
    if ch == "\x03":  # Ctrl-C must still quit a raw-mode console
        return "q"
    return ch.lower()


def _read_key_posix() -> str:  # pragma: no cover - needs a real tty
    # Both modules are POSIX-only, so they are typed as absent on a Windows build
    # of mypy. The import cannot run on Windows either: read_key branches first.
    import termios  # type: ignore[import-not-found,unused-ignore]
    import tty  # type: ignore[import-not-found,unused-ignore]

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if not ch:
            return "q"
        if ch == "\x1b":
            seq = sys.stdin.read(1)
            if seq != "[":
                return ESC
            body = sys.stdin.read(1)
            if body.isdigit():
                body += sys.stdin.read(1)
            return _VT_SPECIAL.get(body, "")
        if ch in ("\r", "\n"):
            return ENTER
        if ch == "\x03":
            return "q"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
