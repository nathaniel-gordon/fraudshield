"""Minimal ANSI toolkit — stdlib only, no curses, no third-party terminal library.

curses is not available on Windows, and a dashboard that only renders on one OS is
not a dashboard. Everything here is plain escape sequences plus width-aware padding,
which works in Windows Terminal, conhost with VT enabled, and any POSIX terminal.

Colour is a property of the *sink*, not of the widget: when output is piped to a file
the codes are omitted entirely so the captured frames stay diffable. The renderer is
therefore a pure function of (state, palette) and the tests exercise it with colour
disabled and compare exact text.
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
from dataclasses import dataclass

CSI = "\x1b["
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

RESET = "0"
BOLD = "1"
DIM = "2"
REVERSE = "7"
FG_RED = "31"
FG_GREEN = "32"
FG_YELLOW = "33"
FG_BLUE = "34"
FG_MAGENTA = "35"
FG_CYAN = "36"
FG_WHITE = "37"
FG_GREY = "90"
FG_BRIGHT_RED = "91"
FG_BRIGHT_GREEN = "92"
FG_BRIGHT_YELLOW = "93"
FG_BRIGHT_CYAN = "96"


@dataclass(frozen=True, slots=True)
class Palette:
    """Whether to emit colour at all. ``Palette(False)`` makes every helper a no-op."""

    enabled: bool = True

    def paint(self, text: str, *codes: str) -> str:
        if not self.enabled or not codes:
            return text
        return f"{CSI}{';'.join(codes)}m{text}{CSI}{RESET}m"

    def risk(self, priority: float) -> str:
        """Colour code for a queue priority — red is "look at this now"."""
        if not self.enabled:
            return ""
        if priority >= 1.5:
            return FG_BRIGHT_RED
        if priority >= 1.0:
            return FG_BRIGHT_YELLOW
        return FG_CYAN


@dataclass(frozen=True, slots=True)
class Charset:
    """Glyphs for the frame. Two variants because not every console speaks UTF-8."""

    tl: str
    tr: str
    bl: str
    br: str
    h: str
    v: str
    lt: str
    rt: str
    cursor: str
    dot: str
    ellipsis: str
    prompt: str


UNICODE = Charset("┌", "┐", "└", "┘", "─", "│", "├", "┤", "▸", "·", "…", "»")
ASCII = Charset("+", "+", "+", "+", "-", "|", "+", "+", ">", "-", "~", ">")

#: Active glyph set. This is process-wide because it is a property of the output
#: device, not of any one widget; :func:`configure_stdout` sets it once at startup.
_ACTIVE: Charset = UNICODE


def active_charset() -> Charset:
    return _ACTIVE


def use_charset(charset: Charset) -> None:
    """Select the glyph set every box and table will draw with."""
    global _ACTIVE
    _ACTIVE = charset


def detect_charset(stream) -> Charset:
    """Pick the richest glyph set the stream can actually encode."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        "".join((UNICODE.tl, UNICODE.h, UNICODE.v, UNICODE.cursor,
                 UNICODE.dot, UNICODE.ellipsis, UNICODE.prompt)).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return ASCII
    return UNICODE


def configure_stdout(stream=None) -> Charset:
    """Make stdout UTF-8 where possible, then choose glyphs to match.

    Windows consoles default to a legacy code page that cannot encode box-drawing
    characters; without this a perfectly correct frame raises ``UnicodeEncodeError``.
    ``errors="replace"`` guarantees the console can never kill the process.
    """
    stream = stream or sys.stdout
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        # A console that refuses the reconfigure gets the ASCII charset below.
        with contextlib.suppress(ValueError, OSError):  # pragma: no cover - host specific
            reconfigure(encoding="utf-8", errors="replace")
    charset = detect_charset(stream)
    use_charset(charset)
    return charset


def visible_len(text: str) -> int:
    """Length of ``text`` as the terminal sees it, ignoring escape sequences."""
    return len(_ANSI_RE.sub("", text))


def fit(text: str, width: int) -> str:
    """Pad or truncate to exactly ``width`` visible columns.

    Truncation is ANSI-naive on purpose: callers truncate *before* painting, which
    keeps this function simple and keeps escape sequences from being cut in half.
    """
    length = visible_len(text)
    if length == width:
        return text
    if length < width:
        return text + " " * (width - length)
    plain = _ANSI_RE.sub("", text)
    return plain[: max(width - 1, 0)] + (_ACTIVE.ellipsis if width > 0 else "")


def strip_ansi(text: str) -> str:
    """Remove every escape sequence — used by tests to assert on plain content."""
    return _ANSI_RE.sub("", text)


def supports_color(stream=None) -> bool:
    """True when the sink is an interactive terminal that can render colour."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    return os.environ.get("TERM", "") != "dumb"


def enable_windows_vt() -> bool:
    """Turn on virtual-terminal processing in a legacy Windows console.

    Windows Terminal already does this; conhost does not, and without it the frames
    render as literal escape gibberish. Failure is non-fatal — the caller falls back
    to colourless output.
    """
    if os.name != "nt":
        return True
    try:  # pragma: no cover - depends on the host console
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


class Box:
    """Single-line box drawing with width-aware content rows."""

    def __init__(self, width: int, palette: Palette, charset: Charset | None = None) -> None:
        self.width = width
        self.palette = palette
        self.charset = charset or active_charset()

    @property
    def inner(self) -> int:
        """Columns available between the vertical borders."""
        return self.width - 4

    def top(self, title: str = "", right: str = "") -> str:
        return self._rule(self.charset.tl, self.charset.tr, title, right)

    def divider(self, title: str = "", right: str = "") -> str:
        return self._rule(self.charset.lt, self.charset.rt, title, right)

    def bottom(self) -> str:
        return self.charset.bl + self.charset.h * (self.width - 2) + self.charset.br

    def row(self, content: str = "") -> str:
        v = self.charset.v
        return f"{v} {fit(content, self.inner)} {v}"

    def _rule(self, left: str, right_char: str, title: str, right: str) -> str:
        h = self.charset.h
        label = f" {title} " if title else ""
        tail = f" {right} " if right else ""
        fill = self.width - 2 - visible_len(label) - visible_len(tail)
        if fill < 2:
            label, fill = "", self.width - 2 - visible_len(tail)
        painted = self.palette.paint(label, BOLD, FG_BRIGHT_CYAN) if label else ""
        painted_tail = self.palette.paint(tail, DIM) if tail else ""
        return left + h + painted + h * max(fill - 1, 0) + painted_tail + right_char


def columns(cells: list[tuple[str, int]], gap: int = 1) -> str:
    """Lay out ``(text, width)`` cells side by side with a fixed gap."""
    return (" " * gap).join(fit(text, width) for text, width in cells)
