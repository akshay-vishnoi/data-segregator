from __future__ import annotations

from collections import Counter, defaultdict

from rich.table import Table

from .review_pack import (
    CANDIDATE_FIELDS,
    CONFLICT_FIELDS,
    SAMPLE_FIELDS,
    SIDECAR_FIELDS,
    UNKNOWN_DATE_FIELDS,
    _category_bytes,
    _markdown_table,
    _read_plan,
    _review_dir,
    _sample_rows,
    _sidecar_rows,
    _utc_now,
    _write_csv_atomic,
    console,
)
from .project import require_project
from .utils import format_size

FORMAT_SUMMARY_FIELDS = [
    "extension",
    "future_copy_files",
    "future_copy_size_bytes",
    "skipped_exact_duplicate_records",
    "skipped_exact_duplicate_size_bytes",
    "total_candidate_source_records",
    "total_candidate_source_size_bytes",
]

COPY_ACTIONS = {"planned-copy", "planned-copy-review"}
CANDIDATE_ACTIONS = COPY_ACTIONS | {"skip-exact-duplicate"}


def _candidate_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        [
            row for row in rows
            if row["media_type"] in {"photo", "video"}
            and row["classification_confidence"] != "definite"
            and row["planned_action"] in CANDIDATE_ACTIONS
        ],
        key=lambda row: (-int(row["size_bytes"]), row["source_relative_path"].casefold()),
    )


def _format_summary(rows: list[dict[str, str]]) -> list[dict[str, str | int]]:
    summary: dict[str, dict[str, int]] = defaultdict(lambda: {
        "future_copy_files": 0,
        "future_copy_size_bytes": 0,
        "skipped_exact_duplicate_records": 0,
        "skipped_exact_duplicate_size_bytes": 0,
        "total_candidate_source_records": 0,
        "total_candidate_source_size_bytes": 0,
    })
    for row in rows:
        extension = row["extension"].lower() or "(no extension)"
        values = summary[extension]
        size = int(row["size_bytes"])
        values["total_candidate_source_records"] += 1
        values["total_candidate_source_size_bytes"] += size
        if row["planned_action"] in COPY_ACTIONS:
            values["future_copy_files"] += 1
            values["future_copy_size_bytes"] += size
        elif row["planned_action"] == "skip-exact-duplicate":
            values["skipped_exact_duplicate_records"] += 1
            values["skipped_exact_duplicate_size_bytes"] += size

    return [
        {"extension": extension, **values}
        for extension, values in sorted(
            summary.items(),
            key=lambda item: (-item[1]["future_copy_size_bytes"], item[0]),
        )
    ]


def build_pre_copy_review_pack(project_name: str) -> dict[str, int]:
    """Create a corrected review pack from the local dry-run plan only."""
    require_project(project_name)
    rows = _read_plan(project_name)
    output_dir = _review_dir(project_name)

    samples = _sample_rows(rows)
    candidates_all = _candidate_rows(rows)
    candidates_future = [row for row in candidates_all if row["planned_action"] in COPY_ACTIONS]
    candidate_formats = _format_summary(candidates_all)

    unknown_dates = sorted(
        [
            row for row in rows
            if row["media_type"] in {"photo", "video"}
            and row["date_source"] == "unknown"
            and row["planned_action"] in CANDIDATE_ACTIONS
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

    all_sidecar_details, _ = _sidecar_rows(rows)
    active_sidecar_ids = {
        row["plan_id"]
        for row in rows
        if row["media_type"] == "sidecar" and row["planned_action"] == "preserve-review-sidecar"
    }
    sidecars = [row for row in all_sidecar_details if row["plan_id"] in active_sidecar_ids]
    sidecars.sort(key=lambda row: row["source_relative_path"].casefold())
    sidecar_statuses = Counter(row["same_folder_companion_status"] for row in sidecars)

    _write_csv_atomic(output_dir / "date_library_samples.csv", SAMPLE_FIELDS, samples)
    _write_csv_atomic(output_dir / "candidate_media_review.csv", CANDIDATE_FIELDS, candidates_all)
    _write_csv_atomic(output_dir / "candidate_media_future_copy.csv", CANDIDATE_FIELDS, candidates_future)
    _write_csv_atomic(output_dir / "candidate_media_format_summary.csv", FORMAT_SUMMARY_FIELDS, candidate_formats)
    _write_csv_atomic(output_dir / "sidecar_companion_review.csv", SIDECAR_FIELDS, sidecars)
    _write_csv_atomic(output_dir / "unknown_date_review.csv", UNKNOWN_DATE_FIELDS, unknown_dates)
    _write_csv_atomic(output_dir / "destination_name_conflicts.csv", CONFLICT_FIELDS, conflicts)

    format_rows = [
        (
            item["extension"],
            f"{item['future_copy_files']:,} future copies / {format_size(item['future_copy_size_bytes'])}; "
            f"{item['skipped_exact_duplicate_records']:,} exact duplicate paths skipped",
        )
        for item in candidate_formats
    ]

    report_lines = [
        "# Pre-Copy Review Pack", "",
        f"Generated: `{_utc_now()}`", "",
        "## Safety", "",
        "- This pack reads the existing local dry-run plan only.",
        "- No source file was opened, copied, moved, renamed, or deleted.",
        "- No destination drive is required or used.", "",
        "## Files Created", "",
        "- `date_library_samples.csv` — 30 deterministic examples: 15 Photos and 15 Videos routed to the main date library.",
        "- `candidate_media_review.csv` — all candidate source records, including byte-identical duplicate paths retained for provenance.",
        "- `candidate_media_future_copy.csv` — only candidate files that would be copied once in the future migration.",
        "- `candidate_media_format_summary.csv` — candidate formats by extension, with future-copy and skipped-duplicate counts/sizes.",
        "- `sidecar_companion_review.csv` — only sidecars still held for companion-file review.",
        "- `unknown_date_review.csv` — photo/video records without a usable date.",
        "- `destination_name_conflicts.csv` — planned safe filename changes.", "",
        "## Summary", "",
    ]
    report_lines.extend(_markdown_table([
        ("Main date-library sample rows", f"{len(samples):,}"),
        ("Candidate source records", f"{len(candidates_all):,} / {format_size(_category_bytes(candidates_all))}"),
        ("Candidate files planned for one future copy", f"{len(candidates_future):,} / {format_size(_category_bytes(candidates_future))}"),
        ("Sidecars held for companion review", f"{len(sidecars):,} / {format_size(_category_bytes(sidecars))}"),
        ("Unknown-date records", f"{len(unknown_dates):,} / {format_size(_category_bytes(unknown_dates))}"),
        ("Destination name conflicts", f"{len(conflicts):,}"),
    ]))
    report_lines.extend(["", "## Candidate Formats", ""])
    report_lines.extend(_markdown_table(format_rows))
    report_lines.extend(["", "## Sidecar Companion Hints", ""])
    report_lines.extend(_markdown_table([
        (status, f"{count:,}")
        for status, count in sidecar_statuses.most_common()
    ]))
    report_lines.extend([
        "", "## How to Review", "",
        "1. Check the date-library samples: source path, recorded date, and planned destination should look sensible.",
        "2. Start with `candidate_media_format_summary.csv`, then review the largest entries in `candidate_media_future_copy.csv`.",
        "3. Exact byte duplicates are already consolidated for a future copy; their original source paths remain in `candidate_media_review.csv`.",
        "4. Sidecars remain held until the dedicated companion-file stage; the hints are not automatic migration decisions.",
    ])
    report_path = output_dir / "pre_copy_review_pack.md"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    table = Table(title="Pre-Copy Review Pack — No Media Files Opened")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Date-library sample rows", f"{len(samples):,}")
    table.add_row("Candidate files planned for copy", f"{len(candidates_future):,}")
    table.add_row("Candidate duplicate paths skipped", f"{len(candidates_all) - len(candidates_future):,}")
    table.add_row("Sidecars held for review", f"{len(sidecars):,}")
    table.add_row("Unknown-date files", f"{len(unknown_dates):,}")
    table.add_row("Filename conflicts", f"{len(conflicts):,}")
    console.print(table)
    console.print(f"[green]Review pack:[/green] {output_dir}")
    console.print("[yellow]No source file was opened, copied, moved, renamed, or deleted.[/yellow]")

    return {
        "samples": len(samples),
        "candidate_future_copy": len(candidates_future),
        "candidate_duplicate_paths_skipped": len(candidates_all) - len(candidates_future),
        "sidecars": len(sidecars),
        "unknown_dates": len(unknown_dates),
        "conflicts": len(conflicts),
    }
