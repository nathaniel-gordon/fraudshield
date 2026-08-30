"""Normalising and checking the paths that arrive from the command line.

Two rules, both learnt the hard way elsewhere:

* a path from an argument is expanded and resolved once, at the edge, so nothing
  downstream has to wonder whether ``~`` or ``..`` is still in it;
* an *input* path must be a local file. ``pandas.read_csv`` happily accepts
  ``https://`` and ``s3://`` URLs, which turns a ``--data`` argument into an outbound
  fetch. This tool reads local exports only.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .errors import InvalidPath

#: Anything that looks like a URL scheme. Windows drive letters (``C:\``) are one
#: character and are excluded by the {2,} bound.
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]{1,}://")


def normalize(raw: str | os.PathLike[str]) -> Path:
    """Expand ``~`` and resolve to an absolute path without touching the disk."""
    text = os.fspath(raw)
    if _SCHEME.match(text):
        raise InvalidPath("remote locations are not supported; pass a local file", path=text)
    return Path(text).expanduser().resolve()


def input_file(raw: str | os.PathLike[str]) -> Path:
    """Resolve a path that must already exist as a readable local file."""
    path = normalize(raw)
    if not path.exists():
        raise InvalidPath("file does not exist", path=str(path))
    if not path.is_file():
        raise InvalidPath("not a regular file", path=str(path))
    return path


def output_file(raw: str | os.PathLike[str], *, create_parent: bool = True) -> Path:
    """Resolve a path to write to, creating its parent directory."""
    path = normalize(raw)
    if path.exists() and path.is_dir():
        raise InvalidPath("path is a directory", path=str(path))
    if create_parent:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


__all__ = ["input_file", "normalize", "output_file"]
