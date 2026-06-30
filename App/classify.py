from __future__ import annotations

from pathlib import Path

from .constants import (
    CANDIDATE_PHOTO_EXTENSIONS,
    CANDIDATE_VIDEO_EXTENSIONS,
    DEFINITE_PHOTO_EXTENSIONS,
    DEFINITE_VIDEO_EXTENSIONS,
    SIDECAR_EXTENSIONS,
)


def is_takeout_sidecar(path: Path) -> bool:
    name = path.name.lower()
    media_extensions = (
        DEFINITE_PHOTO_EXTENSIONS
        | DEFINITE_VIDEO_EXTENSIONS
        | CANDIDATE_PHOTO_EXTENSIONS
        | CANDIDATE_VIDEO_EXTENSIONS
    )
    return any(name.endswith(f"{extension}.json") for extension in media_extensions)


def classify_file(path: Path) -> tuple[str, str] | None:
    """Return (media_type, confidence), or None when out of scope."""
    extension = path.suffix.lower()

    if extension in DEFINITE_PHOTO_EXTENSIONS:
        return "photo", "definite"
    if extension in DEFINITE_VIDEO_EXTENSIONS:
        return "video", "definite"
    if extension in CANDIDATE_PHOTO_EXTENSIONS:
        return "photo", "candidate"
    if extension in CANDIDATE_VIDEO_EXTENSIONS:
        return "video", "candidate"
    if extension in SIDECAR_EXTENSIONS:
        return "sidecar", "companion"
    if extension == ".json" and is_takeout_sidecar(path):
        return "sidecar", "companion"
    return None
