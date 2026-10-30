"""Robust text reading for a corpus with mixed encodings.

eTour was translated from Italian and packaged by hand, so encodings are not
uniform: some files are UTF-8, some UTF-16 (the dataset README is), and some have
stray bytes that are valid in neither. A hard decode error on one file would kill
an entire run, so every read in this project goes through here.
"""

from __future__ import annotations

from pathlib import Path


def read_text(path: str | Path) -> str:
    """Read a text file, tolerating the encoding mess in this corpus.

    Tries UTF-8 (with BOM detection for UTF-16), then falls back to latin-1,
    which cannot fail -- every byte 0-255 maps to a character. Mojibake in a few
    files is a far better outcome than a crashed run.
    """
    raw = Path(path).read_bytes()

    # BOM sniffing. UTF-16 files are common in this dataset and decode to
    # garbage (or raise) if read as UTF-8.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")
