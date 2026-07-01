from __future__ import annotations

import csv
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .review_pack import _read_plan
from .utils import format_size

console = Console()

COPY_ACTIONS = {"planned-copy", "planned-copy-review"}
SIDECAR_EXTENSIONS = {".aae", ".xmp", ".thm"}
EXPECTED_MEDIA_TYPES = {
    ".aae": {"photo"},
    ".xmp": {"photo", "video"},
    ".thm": {"video"},
}

MAPPING_FIELDS = [
    "sidecar_plan_id",
    "sidecar_source_absolute_path",
    "sidecar_source_relative_path",
    "sidecar_filename",
    "sidecar_extension",
    "sidecar_size_bytes",
    "mapping_status",
    "future_copy_decision",
    "mapping_reason",
    "matched_media_count",
    "matched_media_plan_id",
    "matched_media_source_absolute_path",
    "matched_media_source_relative_path",
    "matched_media_filename",
    "matched_media_type",
    "matched_media_planned_action",
    "matched_media_duplicate_role",
    "matched_media_destination",
    "planned_sidecar_destination",
    "all_matching_media_source_paths",
    "all_matching_media_destinations",
]


class SidecarMappingError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _review_dir(project_name: str) -> Path:
    path = project_dir(project_name) / "reviews" / "sidecar_mapping"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalized_parent_and_stem(row: dict[str, str]) -> tuple[str, str, str]:
    source_path = Path(row["source_relative_path"].replace("\\", "/"))
    return (
        row["source_id"],
        str(source_path.parent).casefold(),
        source_path.stem.casefold(),
    )


def _write_csv_atomic(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MAPPING_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _sidecar_destination(media_destination: str, extension: str) -> str:
    if not media_destination:
        return ""
    target = Path(media_destination)
    return str(target.with_suffix(extension.lower())).replace("\\", "/")


def _status_for(sidecar: dict[str, str], matches: list[dict[str, str]]) -> tuple[str, str, str, dict[str, str] | None]:
    extension = sidecar["extension"].lower()
    expected_types = EXPECTED_MEDIA_TYPES.get(extension, {"photo", "video"})

    if not matches:
        return (
            "no-same-folder-stem-match",
            "hold-for-review",
            "No media file with the same folder and filename stem was found.",
            None,
        )

    if len(matches) > 1:
        return (
            "multiple-same-folder-stem-matches",
            "hold-for-review",
            "More than one media file shares this folder and filename stem.",
            None,
        )

    match = matches[0]
    if match["media_type"] not in expected_types:
        return (
            "extension-media-type-review",
            "hold-for-review",
            f"{extension} is not normally paired with {match['media_type']} media.",
            match,
        )

    if match["planned_action"] in COPY_ACTIONS and match["planned_destination_relative_path"]:
        return (
            "safe-one-to-one-match",
            "future-copy-with-matched-media",
            "Exactly one compatible same-folder/same-stem media file is planned for a future copy.",
            match,
        )

    if match["planned_action"] == "skip-exact-duplicate" and match["planned_destination_relative_path"]:
        return (
            "duplicate-media-target-review",
            "hold-for-review",
            "The matching source media is an exact duplicate whose future destination uses a different preferred source path.",
            match,
        )

    return (
        "matched-media-not-planned",
        "hold-for-review",
        "A matching media row exists but has no approved future-copy destination.",
        match,
    )


def _to_mapping_row(sidecar: dict[str, str], matches: list[dict[str, str]]) -> dict[str, str]:
    status, decision, reason, match = _status_for(sidecar, matches)
    destinations = sorted({row["planned_destination_relative_path"] for row in matches if row["planned_destination_relative_path"]})
    source_paths = sorted(row["source_relative_path"] for row in matches)
    planned_sidecar_destination = ""
    if match is not None and status == "safe-one-to-one-match":
        planned_sidecar_destination = _sidecar_destination(
            match["planned_destination_relative_path"],
            sidecar["extension"],
        )

    return {
        "sidecar_plan_id": sidecar["plan_id"],
        "sidecar_source_absolute_path": sidecar["source_absolute_path"],
        "sidecar_source_relative_path": sidecar["source_relative_path"],
        "sidecar_filename": sidecar["filename"],
        "sidecar_extension": sidecar["extension"],
        "sidecar_size_bytes": sidecar["size_bytes"],
        "mapping_status": status,
        "future_copy_decision": decision,
        "mapping_reason": reason,
        "matched_media_count": str(len(matches)),
        "matched_media_plan_id": match["plan_id"] if match else "",
        "matched_media_source_absolute_path": match["source_absolute_path"] if match else "",
        "matched_media_source_relative_path": match["source_relative_path"] if match else "",
        "matched_media_filename": match["filename"] if match else "",
        "matched_media_type": match["media_type"] if match else "",
        "matched_media_planned_action": match["planned_action"] if match else "",
        "matched_media_duplicate_role": match["duplicate_role"] if match else "",
        "matched_media_destination": match["planned_destination_relative_path"] if match else "",
        "planned_sidecar_destination": planned_sidecar_destination,
        "all_matching_media_source_paths": " | ".join(source_paths),
        "all_matching_media_destinations": " | ".join(destinations),
    }


def _write_report(report_path: Path, rows: list[dict[str, str]]) -> None:
    status_counts = Counter(row["mapping_status"] for row in rows)
    status_bytes = Counter()
    extension_counts = Counter(row["sidecar_extension"].lower() for row in rows)
    extension_bytes = Counter()
    for row in rows:
        size = int(row["sidecar_size_bytes"])
        status_bytes[row["mapping_status"]] += size
        extension_bytes[row["sidecar_extension"].lower()] += size

    safe_rows = [row for row in rows if row["mapping_status"] == "safe-one-to-one-match"]
    review_rows = [row for row in rows if row["mapping_status"] != "safe-one-to-one-match"]
    lines = [
        "# Sidecar Mapping Report", "",
        f"Generated: `{_utc_now()}`", "",
        "## Safety", "",
        "- This report reads the existing local destination plan only.",
        "- It does not open, copy, move, rename, or delete source media or sidecar files.",
        "- A safe match is conservative: exactly one compatible media file in the same source folder with the same filename stem, and that media file is already planned for a future copy.", "",
        "## Summary", "",
        f"- Sidecars evaluated: **{len(rows):,}** / **{format_size(sum(int(row['sidecar_size_bytes']) for row in rows))}**",
        f"- Safe one-to-one matches: **{len(safe_rows):,}** / **{format_size(sum(int(row['sidecar_size_bytes']) for row in safe_rows))}**",
        f"- Held for review: **{len(review_rows):,}** / **{format_size(sum(int(row['sidecar_size_bytes']) for row in review_rows))}**", "",
        "## Mapping Status", "", "| Status | Files | Size |", "|---|---:|---:|",
    ]
    for status, count in status_counts.most_common():
        lines.append(f"| {status} | {count:,} | {format_size(status_bytes[status])} |")
    lines.extend(["", "## By Sidecar Extension", "", "| Extension | Files | Size |", "|---|---:|---:|"])
    for extension, count in extension_counts.most_common():
        lines.append(f"| {extension or '(none)'} | {count:,} | {format_size(extension_bytes[extension])} |")
    lines.extend([
        "", "## Future Copy Rule", "",
        "- Only `safe-one-to-one-match` rows are eligible for a later sidecar-copy stage.",
        "- All other rows remain preserved on the original source and stay out of the destination until specifically reviewed.",
        "- Even safe rows are not copied by this report.",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_sidecar_mapping_report(project_name: str) -> dict[str, int]:
    """Create a report-only mapping of active sidecars to planned media destinations."""
    require_project(project_name)
    rows = _read_plan(project_name)
    active_sidecars = [
        row
        for row in rows
        if row["media_type"] == "sidecar"
        and row["planned_action"] == "preserve-review-sidecar"
        and row["extension"].lower() in SIDECAR_EXTENSIONS
    ]

    media_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["media_type"] in {"photo", "video"} and row["scope_status"] == "family-media-candidate":
            media_by_key[_normalized_parent_and_stem(row)].append(row)

    mappings = [
        _to_mapping_row(sidecar, media_by_key.get(_normalized_parent_and_stem(sidecar), []))
        for sidecar in active_sidecars
    ]
    mappings.sort(key=lambda row: row["sidecar_source_relative_path"].casefold())

    output_dir = _review_dir(project_name)
    all_path = output_dir / "sidecar_mapping_all.csv"
    safe_path = output_dir / "sidecar_mapping_safe_one_to_one.csv"
    review_path = output_dir / "sidecar_mapping_hold_for_review.csv"
    report_path = output_dir / "sidecar_mapping_report.md"

    _write_csv_atomic(all_path, mappings)
    _write_csv_atomic(safe_path, [row for row in mappings if row["mapping_status"] == "safe-one-to-one-match"])
    _write_csv_atomic(review_path, [row for row in mappings if row["mapping_status"] != "safe-one-to-one-match"])
    _write_report(report_path, mappings)

    status_counts = Counter(row["mapping_status"] for row in mappings)
    safe_count = status_counts["safe-one-to-one-match"]
    table = Table(title="Sidecar Mapping — Report Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Active sidecars evaluated", f"{len(mappings):,}")
    table.add_row("Safe one-to-one matches", f"{safe_count:,}")
    table.add_row("Held for review", f"{len(mappings) - safe_count:,}")
    for status, count in status_counts.most_common():
        table.add_row(status, f"{count:,}")
    console.print(table)
    console.print(f"[green]Sidecar mapping report:[/green] {report_path}")
    console.print("[yellow]No source or destination file was copied, moved, renamed, or deleted.[/yellow]")

    return {
        "active_sidecars": len(mappings),
        "safe_one_to_one": safe_count,
        "held_for_review": len(mappings) - safe_count,
    }
