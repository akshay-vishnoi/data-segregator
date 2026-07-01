from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from .project import require_project, source_runtime_dir
from .reporting import report_dir
from .utils import format_size

console = Console()

DATE_FIELDS = [
    "source_id",
    "source_label",
    "source_relative_path",
    "filename",
    "extension",
    "media_type",
    "confidence",
    "size_bytes",
    "modified_iso",
    "capture_datetime",
    "capture_date",
    "date_precision",
    "date_source",
    "metadata_tag",
    "metadata_value",
    "filename_value",
    "error",
]

PHOTO_TAG_PRIORITY = (
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
    "XMP:DateCreated",
    "XMP:CreateDate",
    "IPTC:DateCreated",
    "Composite:SubSecDateTimeOriginal",
    "Composite:DateTimeOriginal",
)

VIDEO_TAG_PRIORITY = (
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "QuickTime:TrackCreateDate",
    "QuickTime:ContentCreateDate",
    "Keys:CreationDate",
    "XMP:DateCreated",
    "XMP:CreateDate",
    "EXIF:DateTimeOriginal",
    "EXIF:CreateDate",
)

GENERIC_TAG_PRIORITY = (
    "EXIF:DateTimeOriginal",
    "QuickTime:CreateDate",
    "QuickTime:MediaCreateDate",
    "XMP:DateCreated",
    "XMP:CreateDate",
    "EXIF:CreateDate",
)

FILENAME_DATE_TIME = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})[-_]?"
    r"(?P<month>0[1-9]|1[0-2])[-_]?"
    r"(?P<day>0[1-9]|[12]\d|3[01])"
    r"(?:[T _-]?(?P<hour>[01]\d|2[0-3])[:._-]?"
    r"(?P<minute>[0-5]\d)[:._-]?(?P<second>[0-5]\d))?(?!\d)"
)


class MetadataToolError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def metadata_dir(project_name: str) -> Path:
    path = require_project(project_name)
    del path
    output = Path("runtime") / project_name / "metadata"
    output.mkdir(parents=True, exist_ok=True)
    return output


def metadata_paths(project_name: str) -> tuple[Path, Path, Path, Path]:
    directory = metadata_dir(project_name)
    return (
        directory / "capture_dates.csv",
        directory / "metadata_checkpoint.json",
        directory / "metadata_errors.csv",
        report_dir(project_name) / "capture_date_inventory.md",
    )


def _manifest_paths(project_name: str, source_label: str) -> Path:
    manifest = source_runtime_dir(project_name, source_label) / "manifest.csv"
    if not manifest.exists():
        raise FileNotFoundError(f"No manifest found for source '{source_label}'. Run a source scan first.")
    return manifest


def _source_rows(project_name: str, source_labels: set[str] | None) -> Iterable[tuple[dict, dict[str, str]]]:
    project = require_project(project_name)
    for source in project["sources"]:
        if source_labels and source["label"] not in source_labels:
            continue
        manifest = _manifest_paths(project_name, source["label"])
        with manifest.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                yield source, row


def _manifest_total(project_name: str, source_labels: set[str] | None) -> int:
    total = 0
    project = require_project(project_name)
    for source in project["sources"]:
        if source_labels and source["label"] not in source_labels:
            continue
        manifest = _manifest_paths(project_name, source["label"])
        with manifest.open("r", newline="", encoding="utf-8") as handle:
            total += max(sum(1 for _ in handle) - 1, 0)
    return total


def _row_key(source_id: str, relative_path: str) -> tuple[str, str]:
    return source_id, relative_path


def _existing_keys(capture_dates_path: Path) -> set[tuple[str, str]]:
    if not capture_dates_path.exists():
        return set()
    keys: set[tuple[str, str]] = set()
    with capture_dates_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            source_id = row.get("source_id")
            relative_path = row.get("source_relative_path")
            if source_id and relative_path:
                keys.add(_row_key(source_id, relative_path))
    return keys


def _write_checkpoint(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _require_exiftool() -> str:
    executable = shutil.which("exiftool")
    if not executable:
        raise MetadataToolError(
            "ExifTool is required for photo/video capture-date extraction. "
            "Install it on macOS with: brew install exiftool"
        )
    return executable


def _parse_metadata_datetime(value: object) -> tuple[str, str, str] | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text.startswith("0000:00:00") or text.startswith("0000-00-00"):
        return None

    normalized = text.replace("Z", "+00:00")
    parsers = (
        "%Y:%m:%d %H:%M:%S%z",
        "%Y:%m:%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    )
    parsed: datetime | None = None
    for parser in parsers:
        try:
            parsed = datetime.strptime(normalized, parser)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    if not 1900 <= parsed.year <= datetime.now().year + 1:
        return None

    precision = "datetime" if any(token in text for token in (":", "T", " ")) else "date"
    if precision == "date":
        return "", parsed.date().isoformat(), precision
    return parsed.isoformat(), parsed.date().isoformat(), precision


def _filename_date(filename: str) -> tuple[str, str, str, str] | None:
    stem = Path(filename).stem
    match = FILENAME_DATE_TIME.search(stem)
    if not match:
        return None
    parts = match.groupdict()
    try:
        if parts["hour"] is None:
            date_value = datetime(int(parts["year"]), int(parts["month"]), int(parts["day"]))
            return "", date_value.date().isoformat(), "date", match.group(0)
        date_value = datetime(
            int(parts["year"]),
            int(parts["month"]),
            int(parts["day"]),
            int(parts["hour"]),
            int(parts["minute"]),
            int(parts["second"]),
        )
        return date_value.isoformat(), date_value.date().isoformat(), "datetime", match.group(0)
    except ValueError:
        return None


def _filesystem_date(modified_iso: str) -> tuple[str, str, str] | None:
    try:
        parsed = datetime.fromisoformat(modified_iso)
    except ValueError:
        return None
    if not 1900 <= parsed.year <= datetime.now().year + 1:
        return None
    return parsed.isoformat(), parsed.date().isoformat(), "datetime"


def _metadata_tag_value(record: dict, media_type: str) -> tuple[str, object] | None:
    priorities = PHOTO_TAG_PRIORITY if media_type == "photo" else VIDEO_TAG_PRIORITY if media_type == "video" else GENERIC_TAG_PRIORITY
    for tag in priorities:
        if tag in record:
            return tag, record[tag]
    for tag in GENERIC_TAG_PRIORITY:
        if tag in record:
            return tag, record[tag]
    return None


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
        tag_value = _metadata_tag_value(metadata, row["media_type"])
        if tag_value:
            tag, value = tag_value
            parsed = _parse_metadata_datetime(value)
            if parsed:
                capture_datetime, capture_date, precision = parsed
                result.update({
                    "capture_datetime": capture_datetime,
                    "capture_date": capture_date,
                    "date_precision": precision,
                    "date_source": "photo-exif" if row["media_type"] == "photo" else "video-embedded" if row["media_type"] == "video" else "embedded-metadata",
                    "metadata_tag": tag,
                    "metadata_value": str(value),
                    "error": "",
                })
                return result
            result["metadata_tag"] = tag
            result["metadata_value"] = str(value)
            result["error"] = "metadata date was missing or invalid"

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

    return result


def _run_exiftool(exiftool: str, rows: list[tuple[dict, dict[str, str]]]) -> tuple[dict[str, dict], dict[str, str]]:
    """Run read-only ExifTool once for a batch and map source path to metadata."""
    if not rows:
        return {}, {}
    with tempfile.TemporaryDirectory(prefix="data-segregator-exif-") as temporary_directory:
        temporary = Path(temporary_directory)
        arguments_file = temporary / "files.txt"
        output_file = temporary / "metadata.json"
        arguments_file.write_text(
            "".join(f"{Path(source['path']) / row['source_relative_path']}\n" for source, row in rows),
            encoding="utf-8",
        )
        command = [
            exiftool,
            "-j",
            "-G1",
            "-n",
            "-charset", "filename=UTF8",
            "-api", "LargeFileSupport=1",
            "-EXIF:DateTimeOriginal",
            "-EXIF:CreateDate",
            "-XMP:DateCreated",
            "-XMP:CreateDate",
            "-IPTC:DateCreated",
            "-QuickTime:CreateDate",
            "-QuickTime:MediaCreateDate",
            "-QuickTime:TrackCreateDate",
            "-QuickTime:ContentCreateDate",
            "-Keys:CreationDate",
            "-Composite:SubSecDateTimeOriginal",
            "-Composite:DateTimeOriginal",
            "-@", str(arguments_file),
        ]
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
        output_file.write_text(completed.stdout, encoding="utf-8")
        try:
            records = json.loads(completed.stdout) if completed.stdout.strip() else []
        except json.JSONDecodeError:
            records = []

        metadata_by_path: dict[str, dict] = {}
        for record in records:
            source_file = record.get("SourceFile")
            if isinstance(source_file, str):
                metadata_by_path[str(Path(source_file))] = record

        errors_by_path: dict[str, str] = {}
        stderr = completed.stderr.strip()
        if completed.returncode != 0 and stderr:
            for source, row in rows:
                full_path = str(Path(source["path"]) / row["source_relative_path"])
                if full_path not in metadata_by_path:
                    errors_by_path[full_path] = f"ExifTool exit {completed.returncode}: {stderr[:500]}"
        return metadata_by_path, errors_by_path


def _append_rows(path: Path, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=DATE_FIELDS)
        if write_header:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _append_errors(path: Path, rows: list[dict[str, str]]) -> None:
    error_rows = [row for row in rows if row["error"]]
    if not error_rows:
        return
    fields = ["source_id", "source_label", "source_relative_path", "filename", "error"]
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if write_header:
            writer.writeheader()
        writer.writerows([{field: row[field] for field in fields} for row in error_rows])
        handle.flush()
        os.fsync(handle.fileno())


def _build_report(project_name: str, capture_dates_path: Path, report_path: Path, *, status: str) -> None:
    sources: Counter[str] = Counter()
    source_bytes: Counter[str] = Counter()
    years: Counter[str] = Counter()
    year_bytes: Counter[str] = Counter()
    total = 0
    total_bytes = 0
    errors = 0

    if capture_dates_path.exists():
        with capture_dates_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                total += 1
                size = int(row["size_bytes"])
                total_bytes += size
                source = row["date_source"] or "unknown"
                sources[source] += 1
                source_bytes[source] += size
                year = row["capture_date"][:4] if row["capture_date"] else "Unknown"
                years[year] += 1
                year_bytes[year] += size
                if row["error"]:
                    errors += 1

    lines = [
        "# Capture Date Inventory", "",
        f"Generated: `{_utc_now()}`", "",
        f"- Status: **{status}**",
        f"- Files with date records: **{total:,}**",
        f"- Size represented: **{format_size(total_bytes)}**",
        f"- Records with a metadata/read warning: **{errors:,}**", "",
        "## By Date Source", "", "| Date source | Files | Size |", "|---|---:|---:|",
    ]
    for source, count in sources.most_common():
        lines.append(f"| {source} | {count:,} | {format_size(source_bytes[source])} |")

    lines.extend(["", "## By Capture Year", "", "| Year | Files | Size |", "|---|---:|---:|"])
    ordered_years = sorted(years, key=lambda year: (year == "Unknown", year))
    for year in ordered_years:
        lines.append(f"| {year} | {years[year]:,} | {format_size(year_bytes[year])} |")

    lines.extend([
        "",
        "## Interpretation",
        "",
        "- `photo-exif` and `video-embedded` are embedded file metadata.",
        "- `filename` is used only for clearly parseable YYYYMMDD / YYYY-MM-DD style names.",
        "- `filesystem-modified` is a fallback and is not treated as an original capture date.",
        "- No source file was modified during metadata extraction.",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _show_summary(capture_dates_path: Path, report_path: Path, *, status: str) -> None:
    source_counts: Counter[str] = Counter()
    total = 0
    if capture_dates_path.exists():
        with capture_dates_path.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                total += 1
                source_counts[row["date_source"] or "unknown"] += 1
    table = Table(title="Capture Date Extraction — Report Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Date records", f"{total:,}")
    for source, count in source_counts.most_common():
        table.add_row(source, f"{count:,}")
    table.add_row("Status", status)
    console.print(table)
    console.print(f"[green]Date report:[/green] {report_path}")


def run_metadata_extraction(
    project_name: str,
    *,
    source_labels: set[str] | None = None,
    limit: int | None = None,
    batch_size: int = 500,
) -> tuple[int, int, Path, Path]:
    """Extract report-only capture-date metadata with ExifTool; source files are read only."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1 when provided.")
    if batch_size < 1:
        raise ValueError("batch size must be at least 1.")

    exiftool = _require_exiftool()
    capture_dates_path, checkpoint_path, errors_path, report_path = metadata_paths(project_name)
    existing = _existing_keys(capture_dates_path)
    total_manifest_rows = _manifest_total(project_name, source_labels)
    pending: list[tuple[dict, dict[str, str]]] = []
    processed_now = 0
    interrupted = False
    started = time.perf_counter()

    def flush_batch() -> None:
        nonlocal processed_now, pending
        if not pending:
            return
        metadata_by_path, errors_by_path = _run_exiftool(exiftool, pending)
        output_rows: list[dict[str, str]] = []
        for source, row in pending:
            full_path = str(Path(source["path"]) / row["source_relative_path"])
            selected = _select_date(row, metadata_by_path.get(full_path), errors_by_path.get(full_path, ""))
            output_rows.append({
                "source_id": row["source_id"],
                "source_label": row["source_label"],
                "source_relative_path": row["source_relative_path"],
                "filename": row["filename"],
                "extension": row["extension"],
                "media_type": row["media_type"],
                "confidence": row["confidence"],
                "size_bytes": row["size_bytes"],
                "modified_iso": row["modified_iso"],
                **selected,
            })
        _append_rows(capture_dates_path, output_rows)
        _append_errors(errors_path, output_rows)
        existing.update(_row_key(row["source_id"], row["source_relative_path"]) for _, row in pending)
        processed_now += len(pending)
        pending = []
        _write_checkpoint(checkpoint_path, {
            "status": "running",
            "updated_at": _utc_now(),
            "files_with_date_records": len(existing),
            "processed_in_current_run": processed_now,
            "source_labels": sorted(source_labels) if source_labels else "all",
        })

    console.print("[bold green]Data Segregator — capture-date extraction[/bold green]")
    console.print(f"Project: {project_name}")
    console.print(f"Already recorded: {len(existing):,}")
    console.print("[dim]ExifTool runs in read-only mode. This stage writes only local runtime reports.[/dim]\n")

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.description}[/bold cyan]"),
        BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Reading embedded metadata", total=total_manifest_rows, completed=len(existing))
        try:
            for source, row in _source_rows(project_name, source_labels):
                key = _row_key(row["source_id"], row["source_relative_path"])
                if key in existing:
                    continue
                if limit is not None and processed_now + len(pending) >= limit:
                    break
                pending.append((source, row))
                if len(pending) >= batch_size:
                    flush_batch()
                    elapsed = max(time.perf_counter() - started, 0.001)
                    progress.update(task, advance=processed_now, description=f"Processed {processed_now:,} new files ({processed_now / elapsed:.1f}/s)")
                    processed_now = 0
            flush_batch()
        except KeyboardInterrupt:
            interrupted = True
            console.print("\n[yellow]Stopping safely after the current completed batch...[/yellow]")

    # Count without trusting the earlier in-memory set if an interruption occurred.
    final_existing = _existing_keys(capture_dates_path)
    complete = len(final_existing) >= total_manifest_rows and limit is None and not interrupted
    status = "completed" if complete else "partial"
    _write_checkpoint(checkpoint_path, {
        "status": status,
        "updated_at": _utc_now(),
        "files_with_date_records": len(final_existing),
        "total_manifest_records": total_manifest_rows,
        "source_labels": sorted(source_labels) if source_labels else "all",
    })
    _build_report(project_name, capture_dates_path, report_path, status=status)
    _show_summary(capture_dates_path, report_path, status=status)
    console.print(f"[green]Capture-date CSV:[/green] {capture_dates_path}")
    if status == "partial":
        console.print("[yellow]Run the same command again to continue from the saved local CSV.[/yellow]")
    return len(final_existing), total_manifest_rows, capture_dates_path, report_path
