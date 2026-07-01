from __future__ import annotations

import csv
import hashlib
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .utils import format_size

console = Console()

PLAN_REQUIRED_FIELDS = {
    "plan_id",
    "planned_action",
    "requires_review",
    "review_reasons",
    "source_id",
    "source_label",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "extension",
    "media_type",
    "classification_confidence",
    "size_bytes",
    "sha256",
    "date_source",
    "capture_date",
    "duplicate_role",
    "planned_destination_relative_path",
    "destination_name_action",
}

SAMPLE_FIELDS = [
    "sample_group",
    "sample_rank",
    "plan_id",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "media_type",
    "size_bytes",
    "date_source",
    "capture_date",
    "duplicate_role",
    "planned_destination_relative_path",
    "planned_action",
]

CANDIDATE_FIELDS = [
    "plan_id",
    "planned_action",
    "requires_review",
    "review_reasons",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "extension",
    "media_type",
    "classification_confidence",
    "size_bytes",
    "date_source",
    "capture_date",
    "duplicate_role",
    "planned_destination_relative_path",
]

SIDECAR_FIELDS = [
    "plan_id",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "extension",
    "size_bytes",
    "same_folder_companion_status",
    "matching_media_source_paths",
    "matching_planned_destinations",
    "notes",
]

UNKNOWN_DATE_FIELDS = [
    "plan_id",
    "planned_action",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "extension",
    "media_type",
    "classification_confidence",
    "size_bytes",
    "date_source",
    "capture_date",
    "planned_destination_relative_path",
]

CONFLICT_FIELDS = [
    "plan_id",
    "planned_action",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "size_bytes",
    "duplicate_role",
    "destination_name_action",
    "planned_destination_relative_path",
]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _plan_path(project_name: str) -> Path:
    return project_dir(project_name) / "plans" / "destination_dry_run_plan.csv"


def _review_dir(project_name: str) -> Path:
    path = project_dir(project_name) / "reviews" / "pre_copy_review_pack"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_plan(project_name: str) -> list[dict[str, str]]:
    path = _plan_path(project_name)
    if not path.exists():
        raise FileNotFoundError(
            f"Destination dry-run plan is missing: {path}. Run destination_plan_cli first."
        )

    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = PLAN_REQUIRED_FIELDS - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                "Destination dry-run plan is missing columns: " + ", ".join(sorted(missing))
            )
        return list(reader)


def _write_csv_atomic(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _stable_sample(rows: list[dict[str, str]], limit: int) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(row["plan_id"].encode("utf-8")).hexdigest(),
    )[:limit]


def _sample_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    sample_rows: list[dict[str, str]] = []
    for label, prefix in (("Photos", "Photos/"), ("Videos", "Videos/")):
        eligible = [
            row
            for row in rows
            if row["planned_action"] in {"planned-copy", "planned-copy-review"}
            and row["planned_destination_relative_path"].startswith(prefix)
        ]
        for rank, row in enumerate(_stable_sample(eligible, 15), start=1):
            sample_rows.append({"sample_group": label, "sample_rank": str(rank), **row})
    return sample_rows


def _media_key(row: dict[str, str]) -> tuple[str, str, str]:
    source_path = Path(row["source_relative_path"])
    return (
        row["source_id"],
        str(source_path.parent).casefold(),
        source_path.stem.casefold(),
    )


def _sidecar_rows(rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], Counter[str]]:
    media_by_key: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if row["media_type"] in {"photo", "video"}:
            media_by_key[_media_key(row)].append(row)

    sidecars = [row for row in rows if row["media_type"] == "sidecar"]
    result: list[dict[str, str]] = []
    statuses: Counter[str] = Counter()
    for sidecar in sidecars:
        matches = media_by_key.get(_media_key(sidecar), [])
        destinations = sorted(
            {
                match["planned_destination_relative_path"]
                for match in matches
                if match["planned_destination_relative_path"]
            }
        )
        if len(matches) == 1:
            status = "one-same-folder-stem-match"
            note = "Candidate companion match only; no sidecar will be copied until the companion-file stage."
        elif not matches:
            status = "no-same-folder-stem-match"
            note = "No safe automatic companion match was found in the current plan."
        else:
            status = "multiple-same-folder-stem-matches"
            note = "More than one same-folder/stem media candidate exists; manual companion review is required."
        statuses[status] += 1
        result.append({
            "plan_id": sidecar["plan_id"],
            "source_absolute_path": sidecar["source_absolute_path"],
            "source_relative_path": sidecar["source_relative_path"],
            "filename": sidecar["filename"],
            "extension": sidecar["extension"],
            "size_bytes": sidecar["size_bytes"],
            "same_folder_companion_status": status,
            "matching_media_source_paths": " | ".join(match["source_relative_path"] for match in matches),
            "matching_planned_destinations": " | ".join(destinations),
            "notes": note,
        })
    return result, statuses


def _category_bytes(rows: list[dict[str, str]]) -> int:
    return sum(int(row["size_bytes"]) for row in rows)


def _markdown_table(rows: list[tuple[str, str]]) -> list[str]:
    lines = ["| Item | Value |", "|---|---:|"]
    lines.extend(f"| {item} | {value} |" for item, value in rows)
    return lines


def build_pre_copy_review_pack(project_name: str) -> dict[str, int]:
    """Create local review files from the existing plan only; no media file is opened or changed."""
    require_project(project_name)
    rows = _read_plan(project_name)
    output_dir = _review_dir(project_name)

    samples = _sample_rows(rows)
    candidate_media = sorted(
        [
            row for row in rows
            if row["media_type"] in {"photo", "video"}
            and row["classification_confidence"] != "definite"
            and row["planned_action"] in {"planned-copy", "planned-copy-review", "skip-exact-duplicate"}
        ],
        key=lambda row: (-int(row["size_bytes"]), row["source_relative_path"].casefold()),
    )
    unknown_dates = sorted(
        [
            row for row in rows
            if row["media_type"] in {"photo", "video"}
            and row["date_source"] == "unknown"
            and row["planned_action"] in {"planned-copy", "planned-copy-review", "skip-exact-duplicate"}
        ],
        key=lambda row: (-int(row["size_bytes"]), row["source_relative_path"].casefold()),
    )
    conflicts = sorted(
        [
            row for row in rows
            if row["destination_name_action"]
            not in {"", "preserve-original-name", "uses-preferred-duplicate-destination"}
        ],
        key=lambda row: row["planned_destination_relative_path"].casefold(),
    )
    sidecars, sidecar_statuses = _sidecar_rows(rows)
    sidecars.sort(key=lambda row: row["source_relative_path"].casefold())

    _write_csv_atomic(output_dir / "date_library_samples.csv", SAMPLE_FIELDS, samples)
    _write_csv_atomic(output_dir / "candidate_media_review.csv", CANDIDATE_FIELDS, candidate_media)
    _write_csv_atomic(output_dir / "sidecar_companion_review.csv", SIDECAR_FIELDS, sidecars)
    _write_csv_atomic(output_dir / "unknown_date_review.csv", UNKNOWN_DATE_FIELDS, unknown_dates)
    _write_csv_atomic(output_dir / "destination_name_conflicts.csv", CONFLICT_FIELDS, conflicts)

    extension_counts = Counter(row["extension"].lower() for row in candidate_media)
    report_lines = [
        "# Pre-Copy Review Pack", "",
        f"Generated: `{_utc_now()}`", "",
        "## Safety", "",
        "- This pack reads the existing local dry-run plan only.",
        "- No source file was opened, copied, moved, renamed, or deleted.",
        "- No destination drive is required or used.", "",
        "## Files Created", "",
        "- `date_library_samples.csv` — 30 deterministic examples: 15 Photos and 15 Videos already routed to the main date library.",
        "- `candidate_media_review.csv` — every non-definite photo/video candidate, ordered largest first.",
        "- `sidecar_companion_review.csv` — every sidecar plus conservative same-folder/same-stem companion hints.",
        "- `unknown_date_review.csv` — all photo/video records without any usable date.",
        "- `destination_name_conflicts.csv` — every planned safe filename change.", "",
        "## Summary", "",
    ]
    report_lines.extend(_markdown_table([
        ("Main date-library sample rows", f"{len(samples):,}"),
        ("Candidate media records", f"{len(candidate_media):,} / {format_size(_category_bytes(candidate_media))}"),
        ("Sidecar records", f"{len(sidecars):,} / {format_size(_category_bytes(sidecars))}"),
        ("Unknown-date records", f"{len(unknown_dates):,} / {format_size(_category_bytes(unknown_dates))}"),
        ("Destination name conflicts", f"{len(conflicts):,}"),
    ]))
    report_lines.extend(["", "## Candidate Media by Extension", ""])
    report_lines.extend(_markdown_table([
        (extension or "(no extension)", f"{count:,}")
        for extension, count in extension_counts.most_common()
    ]))
    report_lines.extend(["", "## Sidecar Companion Hints", ""])
    report_lines.extend(_markdown_table([
        (status, f"{count:,}")
        for status, count in sidecar_statuses.most_common()
    ]))
    report_lines.extend([
        "", "## How to Review", "",
        "1. Check the 30 date-library samples: source path, recorded date, and planned destination should look sensible.",
        "2. Review candidate media largest-first. These remain under `Needs Date Review` unless a later media-format step promotes them.",
        "3. Do not manually inspect every exact duplicate. The dry-run plan retains every source path and copies only one byte-identical representative later.",
        "4. Sidecars remain held until the next dedicated companion-file stage; same-folder/stem hints are not automatic migration decisions.",
    ])
    report_path = output_dir / "pre_copy_review_pack.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    table = Table(title="Pre-Copy Review Pack — No Media Files Opened")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Date-library sample rows", f"{len(samples):,}")
    table.add_row("Candidate media", f"{len(candidate_media):,}")
    table.add_row("Sidecars", f"{len(sidecars):,}")
    table.add_row("Unknown-date files", f"{len(unknown_dates):,}")
    table.add_row("Filename conflicts", f"{len(conflicts):,}")
    console.print(table)
    console.print(f"[green]Review pack:[/green] {output_dir}")
    console.print("[yellow]No source file was opened, copied, moved, renamed, or deleted.[/yellow]")

    return {
        "samples": len(samples),
        "candidate_media": len(candidate_media),
        "sidecars": len(sidecars),
        "unknown_dates": len(unknown_dates),
        "conflicts": len(conflicts),
    }
