from __future__ import annotations

import time
from pathlib import Path

from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .metadata import (
    MetadataToolError,
    _append_errors,
    _append_rows,
    _build_report,
    _existing_keys,
    _manifest_total,
    _require_exiftool,
    _row_key,
    _run_exiftool,
    _select_date,
    _show_summary,
    _source_rows,
    _write_checkpoint,
    console,
    metadata_paths,
)
from .project import require_project


def _selected_sources(project_name: str, source_labels: set[str] | None) -> tuple[list[dict], set[str]]:
    project = require_project(project_name)
    sources = project["sources"]
    known_labels = {source["label"] for source in sources}
    unknown_labels = (source_labels or set()) - known_labels
    if unknown_labels:
        raise ValueError(f"Unknown source label(s): {', '.join(sorted(unknown_labels))}")
    selected = [source for source in sources if not source_labels or source["label"] in source_labels]
    return selected, {source["id"] for source in selected}


def run_metadata_extraction(
    project_name: str,
    *,
    source_labels: set[str] | None = None,
    limit: int | None = None,
    batch_size: int = 500,
) -> tuple[int, int, Path, Path]:
    """Read capture-date metadata into local reports, safely resumable by source path."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be at least 1 when provided.")
    if batch_size < 1:
        raise ValueError("--batch-size must be at least 1.")

    exiftool = _require_exiftool()
    selected_sources, selected_ids = _selected_sources(project_name, source_labels)
    capture_dates_path, checkpoint_path, errors_path, report_path = metadata_paths(project_name)
    existing = _existing_keys(capture_dates_path)
    total_manifest_rows = _manifest_total(project_name, source_labels)
    selected_existing = sum(1 for source_id, _ in existing if source_id in selected_ids)
    pending: list[tuple[dict, dict[str, str]]] = []
    processed_this_run = 0
    interrupted = False
    started = time.perf_counter()

    def flush_batch() -> int:
        nonlocal pending, processed_this_run
        if not pending:
            return 0
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
        added = len(pending)
        processed_this_run += added
        pending = []
        _write_checkpoint(checkpoint_path, {
            "status": "running",
            "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            "files_with_date_records": len(existing),
            "processed_in_current_run": processed_this_run,
            "source_labels": sorted(source_labels) if source_labels else "all",
        })
        return added

    console.print("[bold green]Data Segregator — capture-date extraction[/bold green]")
    console.print(f"Project: {project_name}")
    console.print(f"Already recorded for selected sources: {selected_existing:,}")
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
        task = progress.add_task("Reading embedded metadata", total=total_manifest_rows, completed=selected_existing)
        try:
            for source, row in _source_rows(project_name, source_labels):
                key = _row_key(row["source_id"], row["source_relative_path"])
                if key in existing:
                    continue
                if limit is not None and processed_this_run + len(pending) >= limit:
                    break
                pending.append((source, row))
                if len(pending) >= batch_size:
                    added = flush_batch()
                    elapsed = max(time.perf_counter() - started, 0.001)
                    progress.update(task, advance=added, description=f"Processed {processed_this_run:,} new files ({processed_this_run / elapsed:.1f}/s)")
            added = flush_batch()
            if added:
                elapsed = max(time.perf_counter() - started, 0.001)
                progress.update(task, advance=added, description=f"Processed {processed_this_run:,} new files ({processed_this_run / elapsed:.1f}/s)")
        except KeyboardInterrupt:
            interrupted = True
            console.print("\n[yellow]Stopped safely after completed metadata batches. Rerun to continue.[/yellow]")

    final_existing = _existing_keys(capture_dates_path)
    final_selected = sum(1 for source_id, _ in final_existing if source_id in selected_ids)
    status = "completed" if final_selected >= total_manifest_rows and limit is None and not interrupted else "partial"
    _write_checkpoint(checkpoint_path, {
        "status": status,
        "updated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "files_with_date_records": len(final_existing),
        "files_with_date_records_for_selected_sources": final_selected,
        "total_manifest_records_for_selected_sources": total_manifest_rows,
        "source_labels": sorted(source_labels) if source_labels else "all",
    })
    _build_report(project_name, capture_dates_path, report_path, status=status)
    _show_summary(capture_dates_path, report_path, status=status)
    console.print(f"[green]Capture-date CSV:[/green] {capture_dates_path}")
    if status == "partial":
        console.print("[yellow]Run the same command again to continue from the saved local CSV.[/yellow]")
    return final_selected, total_manifest_rows, capture_dates_path, report_path


__all__ = ["MetadataToolError", "run_metadata_extraction"]
