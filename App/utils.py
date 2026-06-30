from __future__ import annotations

import re
from pathlib import Path


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("Name must contain at least one letter or number.")
    return slug


def format_size(size_bytes: int | float) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def shorten_path(value: str, max_length: int = 72) -> str:
    if len(value) <= max_length:
        return value
    return "…" + value[-(max_length - 1):]


def resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def paths_overlap(first: Path, second: Path) -> bool:
    first = first.resolve()
    second = second.resolve()
    return first == second or first in second.parents or second in first.parents
