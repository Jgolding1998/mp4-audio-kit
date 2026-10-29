"""Safe display filenames. Names are only used for downloads, never as paths."""

from __future__ import annotations

import unicodedata

_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)}
_KEEP = set(" -_.,()[]&'+!")


def safe_stem(text: str, fallback: str, max_len: int = 80) -> str:
    """Reduce untrusted text to a short, portable filename stem."""
    text = unicodedata.normalize("NFKC", text or "")
    out = []
    for ch in text:
        if unicodedata.category(ch)[0] == "C":
            continue  # control, format (bidi overrides), private use
        out.append(ch if (ch.isalnum() or ch in _KEEP) else "_")
    stem = "".join(out)
    while "__" in stem:
        stem = stem.replace("__", "_")
    stem = " ".join(stem.split())[:max_len].strip(" ._-")
    if not stem:
        return fallback
    if stem.split(".")[0].strip().upper() in _RESERVED:
        stem = "_" + stem
    return stem
