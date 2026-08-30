"""Drivers for the dashboard: an interactive loop and a scripted, self-terminating one.

The two share the dashboard entirely — same state machine, same renderer, same
commands. They differ only in the source of keystrokes and where frames are written.
That is deliberate: a demo that exercised a different code path from the real UI would
prove nothing about the real UI.

The scripted runner always terminates: it consumes a finite key script and stops, and
it never reads stdin.
"""
from __future__ import annotations

import sys
from collections.abc import Iterable, Sequence
from typing import TextIO

from .ansi import CSI, Palette, enable_windows_vt, supports_color
from .dashboard import Dashboard
from .keys import read_key

#: A tour that visits every view and dispositions the top of the queue three ways.
#: Ends in "q" so the runner stops on its own even if the caller forgets a limit.
DEMO_SCRIPT: tuple[str, ...] = (
    "j", "j", "j",           # walk down the queue
    "a",                     # audit trail for the selected case
    "1",                     # back to the queue
    "t",                     # time travel: what did we know when it opened?
    "1",
    "f",                     # confirm fraud on the top case
    "f",                     # and the next one
    "g",                     # mark one as a false alert
    "x",                     # escalate one
    "r",                     # rule performance, now that dispositions exist
    "m",                     # daily metrics
    "c",                     # counterfactual policy sweep
    "1",
    "q",
)


def run_scripted(
    dashboard: Dashboard,
    script: Sequence[str] = DEMO_SCRIPT,
    *,
    out: TextIO | None = None,
    show_frames: Iterable[int] | None = None,
) -> list[list[str]]:
    """Drive the dashboard from a fixed key script and return every frame rendered.

    Args:
        dashboard: the console to drive.
        script: keystrokes to apply, in order.
        out: where to print frames; ``None`` renders silently (used by tests).
        show_frames: indices of frames to print; ``None`` prints all of them.

    Returns:
        One list of lines per frame, including the initial frame before any key.
    """
    frames = [dashboard.render()]
    keys_applied = ["(initial)"]
    for key in script:
        if not dashboard.handle(key):
            break
        frames.append(dashboard.render())
        keys_applied.append(key)
    if out is not None:
        wanted = set(range(len(frames))) if show_frames is None else set(show_frames)
        for index, (frame, key) in enumerate(zip(frames, keys_applied, strict=True)):
            if index not in wanted:
                continue
            out.write(f"\n  frame {index + 1}/{len(frames)}  key={key}\n")
            out.write("\n".join(frame) + "\n")
    return frames


def run_live(dashboard: Dashboard, *, out: TextIO | None = None) -> int:
    """Interactive loop: clear, draw, read one key, repeat until the analyst quits.

    Returns:
        The number of dispositions recorded during the session.
    """
    stream = out or sys.stdout
    colour = supports_color(stream) and enable_windows_vt()
    dashboard.set_palette(Palette(colour))
    try:
        stream.write(f"{CSI}?25l")  # hide the cursor; the frame is the cursor
        while True:
            stream.write(f"{CSI}2J{CSI}H")
            stream.write("\n".join(dashboard.render()) + "\n")
            stream.flush()
            if not dashboard.handle(read_key()):
                break
    finally:
        stream.write(f"{CSI}?25h{CSI}0m\n")
        stream.flush()
    return dashboard.state.dispositions
