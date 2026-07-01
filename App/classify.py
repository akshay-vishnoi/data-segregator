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


def looks_like_mpeg_transport_stream(path: Path) -> bool | None:
    """Return True for a recognizable MPEG transport stream, False for non-TS, or None on read failure.

    The .ts suffix is ambiguous: it is often TypeScript source code. MPEG transport streams
    normally contain repeating 0x47 synchronization bytes at fixed packet intervals.
    """
    packet_sizes = (188, 192, 204)
    packets_required = 3
    sample_size = max(packet_sizes) * (packets_required + 1)
    try:
        with path.open("rb") as handle:
            sample = handle.read(sample_size)
    except OSError:
        return None

    for packet_size in packet_sizes:
        required_bytes = packet_size * packets_required
        if len(sample) < required_bytes:
            continue
        for offset in range(packet_size):
            positions = [offset + packet_size * index for index in range(packets_required)]
            if positions[-1] >= len(sample):
                break
            if all(sample[position] == 0x47 for position in positions):
                return True
    return False


def classify_file(path: Path) -> tuple[str, str] | None:
    """Return (media_type, confidence), or None when out of scope."""
    extension = path.suffix.lower()

    # .ts is deliberately inspected before generic candidate-video matching because
    # TypeScript source files use the same suffix. A file that cannot be opened stays
    # a candidate rather than being silently excluded.
    if extension == ".ts":
        is_transport_stream = looks_like_mpeg_transport_stream(path)
        if is_transport_stream is True or is_transport_stream is None:
            return "video", "candidate"
        return None
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
