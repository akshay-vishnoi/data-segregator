from __future__ import annotations

DEFINITE_PHOTO_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif",
    ".dng", ".cr2", ".nef", ".arw", ".orf", ".rw2", ".pef", ".srw",
})

DEFINITE_VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".3g2", ".mts", ".m2ts",
})

CANDIDATE_PHOTO_EXTENSIONS = frozenset({
    ".webp", ".avif", ".bmp", ".tif", ".tiff", ".jfif", ".raw",
})

# .dat intentionally remains here because it can contain real older family videos.
CANDIDATE_VIDEO_EXTENSIONS = frozenset({
    ".dat", ".vob", ".ts", ".mpg", ".mpeg", ".wmv", ".flv", ".asf", ".dv", ".hevc", ".webm",
})

SIDECAR_EXTENSIONS = frozenset({".aae", ".xmp", ".thm"})

ALL_KNOWN_MEDIA_EXTENSIONS = (
    DEFINITE_PHOTO_EXTENSIONS
    | DEFINITE_VIDEO_EXTENSIONS
    | CANDIDATE_PHOTO_EXTENSIONS
    | CANDIDATE_VIDEO_EXTENSIONS
    | SIDECAR_EXTENSIONS
    | {".json"}
)

MANIFEST_FIELDS = [
    "source_id",
    "source_label",
    "source_relative_path",
    "filename",
    "extension",
    "media_type",
    "confidence",
    "size_bytes",
    "modified_epoch",
    "modified_iso",
    "discovered_at",
]

MANIFEST_FLUSH_EVERY = 2_000
PROGRESS_REFRESH_EVERY = 100
HASH_CHUNK_BYTES = 8 * 1024 * 1024
