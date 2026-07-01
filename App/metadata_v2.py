from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from .metadata import (
    MetadataToolError,
    _append_errors,
    _append_rows,
    _existing_keys,
    _filename_date,
    _filesystem_date,
    _require_exiftool,
    _row_key,
    _run_exiftool,
    _write_checkpoint,
)
from .project import project_dir, require_project, source_runtime_dir
from .reporting import report_dir
from .utils import format_size

console = Console()

# Use actual ExifTool -G1 JSON group names. The code also matches the suffix as a
# safeguard for group variations such as Track1, Track2, or vendor XMP namespaces.
PHOTO_TAGS = (
    "ExifIFD:DateTimeOriginal",
    "EXIF:DateTimeOriginal",
    "Composite:SubSecDateTimeOriginal",
    "Composite:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "EXIF:CreateDate",
    "XMP-photoshop:DateCreated",
    "XMP-xmp:DateCreated",
    "XMP:DateCreated",
    "XMP-xmp:CreateDate",
    "XMP:CreateDate",
    "IPTC:DateCreated",
)

# Keys:CreationDate is intentionally first: Apple media often supplies it with a
# time-zone offset, while QuickTime's corresponding value may be a UTC-looking
# representation of the same instant and can fall on a different calendar day.
VIDEO_TAGS = (
    "Keys:CreationDate",
    "XMP-photoshop:DateCreated",
    "XMP-xmp:DateCreated",
    "XMP:DateCreated",
    "XMP-xmp:CreateDate",
    "XMP:CreateDate",
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:TrackCreateDate",
    "Track1:MediaCreateDate",
    "Track1:TrackCreateDate",
    "ExifIFD:DateTimeOriginal",
    "EXIF:DateTimeOriginal",
    "ExifIFD:CreateDate",
    "EXIF:CreateDate",
)

SUFFIX_FALLBACKS = {
    "photo": (
        "DateTimeOriginal",
        "SubSecDateTimeOriginal",
        "CreateDate",
        "DateCreated",
    ),
    "video": (
        "CreationDate",
        "DateCreated",
        "CreateDate",
        "MediaCreateDate",
        "TrackCreateDate",
        "DateTimeOriginal",
    ),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(project_name: str) -> tuple[Path, Path, Path, Path]:
    require_project(project_name)
    directory = project_dir(project_name) / "metadata-v2"
    directory.mkdir(parents=True, exist_ok=True)
    return (
        directory / "capture_dates.csv",
        directory / "metadata_checkpoint.json",
        directory / "metadata_errors.csv",
        report_dir(project_name) / "capture_date_inventory_v2.md",
    )


def _selected_sources(project_name: str, source_labels: set[str] | None) -> list[dict]:
    project = require_project(project_name)
    known = {source["label"] for source in project["sources"]}
    unknown = (source_labels or set()) - known
    if unknown:
        raise ValueError(f"Unknown source label(s): {', '.join(sorted(unknown))}")
    return [source for source in project["sources"] if not source_labels or source["label"] in source_labels]


def _source_rows(project_name: str, sources: list[dict], only_paths: set[str] | None) -> Iterable[tuple[dict, dict[str, str]]]:
    for source in sources:
        manifest = source_runtime_dir(project_name, source["label"]) / "manifest.csv"
        if not manifest.exists():
            raise FileNotFoundError(f"No manifest found for source '{source['label']}'. Run a source scan first.")
        with manifest.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if only_paths and row["source_relative_path"] not in only_paths:
                    continue
                yield source, row


def _manifest_total(project_name: str, sources: list[dict], only_paths: set[str] | None) -> int:
    return sum(1 for _ in _source_rows(project_name, sources, only_paths))


def _tag_value(metadata: dict, media_type: str) -> tuple[str, object] | None:
    priorities = PHOTO_TAGS if media_type == "photo" else VIDEO_TAGS
    for tag in priorities:
        value = metadata.get(tag)
        if value not in (None, ""):
            return tag, value

    for suffix in SUFFIX_FALLBACKS.get(media_type, ("CreateDate", "DateCreated")):
        for tag, value in metadata.items():
            if tag.endswith(f":{suffix}") and value not in (None, ""):
                return tag, value
    return None


def _parse_metadata_datetime(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0000:00:00") or text.startswith("0000-00-00"):
        return None

    normalized = text.replace("Z", "+00:00")
    formats = (
        "%Y:%m:%d %H:%M:%S%z",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )
    parsed: datetime | None = None
    for fmt in formats:
        try:
            parsed = datetime.strptime(normalized, fmt)
            break
        except ValueError:
            continue
    if parsed is None or not 1900 <= parsed.year <= datetime.now().year + 1:
        return None

    has_time = any(marker in text for marker in (":", "T", " "))
    if not has_time:
        return "", parsed.date().isoformat(), "date"
    return parsed.isoformat(), parsed.date().isoformat(), "datetime"


def _select_date(row: dict[str, str], metadata: dict | None, metadata_error: str) -> dict[str, str]:
    result = {
        "capture_datetime": "",
        "capture_date": "",
        "date_precision": "",
        "date_source": "unknown",
        "metadata_tag": "",
        "metadata_value": "",
        "filename_value": "",
        "error": metadata_error,
    }

    if metadata:
        candidate = _tag_value(metadata, row["media_type"])
        if candidate:
            tag, value = candidate
            parsed = _parse_metadata_datetime(value)
            if parsed:
                capture_datetime, capture_date, precision = parsed
                result.update({
                    "capture_datetime": capture_datetime,
                    "capture_date": capture_date,
                    "date_precision": precision,
                    "date_source": "photo-exif" if row["media_type"] == "photo" else "video-embedded",
                    "metadata_tag": tag,
                    "metadata_value": str(value),
                    "error": "",
                })
                return result
            result.update({
                "metadata_tag": tag,
                "metadata_value": str(value),
                "error": "metadata date was missing or invalid",
            })

    filename_date = _filename_date(row["filename"])
    if filename_date:
        capture_datetime, capture_date, precision, filename_value = filename_date
        result.update({
            "capture_datetime": capture_datetime,
            "capture_date": capture_date,
            "date_precision": precision,
            "date_source": "filename",
            "filename_value": filename_value,
        })
        return result

    filesystem_date = _filesystem_date(row["modified_iso"])
    if filesystem_date:
        capture_datetime, capture_date, precision = filesystem_date
        result.update({
            "capture_datetime": capture_datetime,
            "capture_date": capture_date,
            "date_precision": precision,
            "date_source": "filesystem-modified",
        })
    return result


def _build_report(capture_dates_path: Path, report_path: Path, status: str) -> None:
    source_counts: Counter[str] = Counter()
    source_bytes: Counter[str] = Counter()
    total = 0
    total_bytes = 0
    warnings = 0
    if capture_dates_path.exists():
        with capture_dates_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                total += 1
                size = int(row["size_bytes"])
                total_bytes += size
                source = row["date_source"] or "unknown"
                source_counts[source] += 1
                source_bytes[source] += size
                warnings += 1 if row["error"] else 0

    lines = [
        "# Capture Date Inventory", "",
        f"Generated: `{_utc_now()}`", "",
        f"- Status: **{status}**",
        f"- Files with date records: **{total:,}**",
        f"- Size represented: **{format_size(total_bytes)}**",
        f"- Records with a metadata/read warning: **{warnings:,}**", "",
        "## By Date Source", "", "| Date source | Files | Size |", "|---|---:|---:|",
    ]
    for source, count in source_counts.most_common():
        lines.append(f"| {source} | {count:,} | {format_size(source_bytes[source])} |")
    lines.extend([
        "", "## Interpretation", "",
        "- `photo-exif` and `video-embedded` come from embedded file metadata.",
        "- `Keys:CreationDate` is preferred for Apple videos when it includes a time-zone offset.",
        "- `filesystem-modified` is a labeled fallback, not an asserted original capture date.",
        "- No source file was modified during metadata extraction.",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _show_summary(capture_dates_path: Path, report_path: Path, status: str) -> None:
    counts: Counter[str] = Counter()
    total = 0
    with capture_dates_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            total += 1
            counts[row["date_source"] or "unknown"] += 1
    table = Table(title="Capture Date Extraction V2 — Report Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Date records", f"{total:,}")
    for name, count in counts.most_common():
        table.add_row(name, f"{count:,}")
    table.add_row("Status", status)
    console.print(table)
    console.print(f"[green]Date report:[/green] {report_path}")


def run_metadata_extraction(
    project_name: str,
    *,
    source_labels: set[str] | None = None,
    limit: int | None = None,
    batch_size: int = 500,
    paths: set[str] | None = None,
) -> tuple[int, int, Path, Path]:
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1 when provided.")
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

    exiftool = _require_exiftool()
    sources = _selected_sources(project_name, source_labels)
    capture_dates_path, checkpoint_path, errors_path, report_path = _paths(project_name)
    existing = _existing_keys(capture_dates_path)
    total = _manifest_total(project_name, sources, paths)
    source_ids = {source["id"] for source in sources}
    selected_existing = sum(1 for source_id, relative_path in existing if source_id in source_ids and (not paths or relative_path in paths))
    pending: list[tuple[dict, dict[str, str]]] = []
    processed = 0
    interrupted = False
    started = time.perf_counter()

    def flush() -> int:
        nonlocal pending, processed
        if not pending:
            return 0
        metadata_by_path, errors_by_path = _run_exiftool(exiftool, pending)
        output: list[dict[str, str]] = []
        for source, row in pending:
            full_path = str(Path(source["path"]) / row["source_relative_path"])
            output.append({
                "source_id": row["source_id"],
                "source_label": row["source_label"],
                "source_relative_path": row["source_relative_path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "media_type": row["media_type"],
                "confidence": row["confidence"],
                "size_bytes": row["size_bytes"],
                "modified_iso": row["modified_iso"],
                **_select_date(row, metadata_by_path.get(full_path), errors_by_path.get(full_path, "")),
            })
        _append_rows(capture_dates_path, output)
        _append_errors(errors_path, output)
        existing.update(_row_key(row["source_id"], row["source_relative_path"]) for _, row in pending)
        added = len(pending)
        processed += added
        pending = []
        _write_checkpoint(checkpoint_path, {
            "status": "running",
            "updated_at": _utc_now(),
            "date_records": len(existing),
            "processed_this_run": processed,
            "source_labels": sorted(source_labels) if source_labels else "all",
        })
        return added

    console.print("[bold green]Data Segregator — capture-date extraction V2[/bold green]")
    console.print(f"Project: {project_name}")
    console.print(f"Already recorded for this selection: {selected_existing:,}")
    console.print("[dim]ExifTool runs in read-only mode. This writes only local runtime CSV/report files.[/dim]\n")

    with Progress(
        SpinnerColumn(), TextColumn("[bold cyan]{task.description}[/bold cyan]"), BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"), TimeElapsedColumn(), TimeRemainingColumn(), console=console,
    ) as progress:
        task = progress.add_task("Reading embedded metadata", total=total, completed=selected_existing)
        try:
            for source, row in _source_rows(project_name, sources, paths):
                key = _row_key(row["source_id"], row["source_relative_path"])
                if key in existing:
                    continue
                if limit is not None and processed + len(pending) >= limit:
                    break
                pending.append((source, row))
                if len(pending) >= batch_size:
                    added = flush()
                    elapsed = max(time.perf_counter() - started, 0.001)
                    progress.update(task, advance=added, description=f"Processed {processed:,} new files ({processed / elapsed:.1f}/s)")
            added = flush()
            if added:
                elapsed = max(time.perf_counter() - started, 0.001)
                progress.update(task, advance=added, description=f"Processed {processed:,} new files ({processed / elapsed:.1f}/s)")
        except KeyboardInterrupt:
            interrupted = True
            console.print("\n[yellow]Stopped safely after completed metadata batches. Rerun to continue.[/yellow]")

    latest = _existing_keys(capture_dates_path)
    selected_total = sum(1 for source_id, relative_path in latest if source_id in source_ids and (not paths or relative_path in paths))
    complete = selected_total >= total and limit is None and not interrupted
    status = "completed" if complete else "partial"
    _write_checkpoint(checkpoint_path, {
        "status": status,
        "updated_at": _utc_now(),
        "date_records": len(latest),
        "date_records_for_selection": selected_total,
        "records_in_selection": total,
        "source_labels": sorted(source_labels) if source_labels else "all",
    })
    _build_report(capture_dates_path, report_path, status)
    _show_summary(capture_dates_path, report_path, status)
    console.print(f"[green]Capture-date CSV:[/green] {capture_dates_path}")
    if status == "partial":
        console.print("[yellow]Run the same command again to continue from the saved local CSV.[/yellow]")
    return selected_total, total, capture_dates_path, report_path
