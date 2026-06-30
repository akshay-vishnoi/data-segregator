from __future__ import annotations

import csv
import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from .classify import classify_file
from .constants import ALL_KNOWN_MEDIA_EXTENSIONS, MANIFEST_FIELDS, MANIFEST_FLUSH_EVERY, PROGRESS_REFRESH_EVERY
from .project import get_source, source_runtime_dir, update_source_status
from .utils import format_size, shorten_path

console = Console()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def source_paths(project_name: str, label: str) -> tuple[dict, dict, Path, Path, Path, Path]:
    project, source = get_source(project_name, label)
    runtime = source_runtime_dir(project_name, label)
    manifest = runtime / "manifest.csv"
    checkpoint = runtime / "checkpoint.json"
    summary = runtime / "scan_summary.md"
    log = runtime / "scan_errors.log"
    runtime.mkdir(parents=True, exist_ok=True)
    return project, source, manifest, checkpoint, summary, log


def load_manifest(manifest_path: Path) -> tuple[set[str], Counter, Counter, Counter, int, int]:
    existing: set[str] = set()
    extension_counts: Counter = Counter()
    type_counts: Counter = Counter()
    confidence_counts: Counter = Counter()
    total_size = 0
    total_records = 0

    if not manifest_path.exists():
        return existing, extension_counts, type_counts, confidence_counts, total_size, total_records

    with manifest_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            relative_path = row.get("source_relative_path")
            if not relative_path:
                continue
            existing.add(relative_path)
            total_records += 1
            extension_counts[row.get("extension", "")] += 1
            type_counts[row.get("media_type", "")] += 1
            confidence_counts[row.get("confidence", "")] += 1
            try:
                total_size += int(row.get("size_bytes", 0))
            except ValueError:
                pass

    return existing, extension_counts, type_counts, confidence_counts, total_size, total_records


def write_json_atomically(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_checkpoint(checkpoint: Path, *, status: str, source_path: Path, total_records: int, total_size: int, current_path: str, errors: int) -> None:
    write_json_atomically(checkpoint, {
        "status": status,
        "updated_at": utc_now(),
        "source_path": str(source_path),
        "records_discovered": total_records,
        "media_size_bytes": total_size,
        "current_path": current_path,
        "errors": errors,
    })


def write_summary(summary: Path, *, status: str, source: dict, total_records: int, total_size: int, errors: int, extension_counts: Counter, type_counts: Counter, confidence_counts: Counter) -> None:
    lines = [
        "# Source Scan Summary",
        "",
        f"- Status: **{status}**",
        f"- Generated: `{utc_now()}`",
        f"- Source label: `{source['label']}`",
        f"- Source path: `{source['path']}`",
        f"- Total selected records: **{total_records:,}**",
        f"- Total selected size: **{format_size(total_size)}**",
        f"- Errors: **{errors:,}**",
        "",
        "## By Media Type",
        "",
        "| Type | Files |",
        "|---|---:|",
    ]
    lines.extend(f"| {name} | {count:,} |" for name, count in type_counts.most_common())
    lines.extend(["", "## By Confidence", "", "| Confidence | Files |", "|---|---:|"])
    lines.extend(f"| {name} | {count:,} |" for name, count in confidence_counts.most_common())
    lines.extend(["", "## By Extension", "", "| Extension | Files |", "|---|---:|"])
    lines.extend(f"| {name} | {count:,} |" for name, count in extension_counts.most_common())
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")


def flush_batch(handle, writer: csv.DictWriter, batch: list[dict]) -> None:
    if not batch:
        return
    writer.writerows(batch)
    handle.flush()
    batch.clear()


def scan_source(project_name: str, source_label: str) -> None:
    project, source, manifest, checkpoint, summary, error_log = source_paths(project_name, source_label)
    root = Path(source["path"])
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(
            f"Source '{source_label}' is unavailable at {root}. Use source relink first."
        )

    checkpoint_status = None
    if checkpoint.exists():
        try:
            checkpoint_status = json.loads(checkpoint.read_text(encoding="utf-8")).get("status")
        except (OSError, json.JSONDecodeError):
            checkpoint_status = None

    if manifest.exists() and checkpoint_status == "completed":
        console.print("[green]This source already has a completed manifest.[/green]")
        console.print("Add a new source for newly found files, or delete the source manifest only if you intentionally want a fresh scan.")
        return

    existing_paths, extension_counts, type_counts, confidence_counts, total_size, total_records = load_manifest(manifest)
    resumed = bool(existing_paths)
    source_mode = "a" if manifest.exists() else "w"
    batch: list[dict] = []
    new_records = 0
    errors = 0
    current_path = "-"
    interrupted = False
    started = time.perf_counter()

    console.print("[bold green]Family Media Separation — source scan[/bold green]")
    console.print(f"Project: {project['slug']}")
    console.print(f"Source: {source['label']} → {root}")
    if resumed:
        console.print(f"[yellow]Resume mode:[/yellow] {len(existing_paths):,} saved records")
    console.print("[dim]No percentage is shown because an accurate percentage requires a second full disk walk. The scan shows live count, size, rate, and current path.[/dim]\n")

    update_source_status(project, source["id"], "running")

    with manifest.open(source_mode, newline="", encoding="utf-8") as manifest_handle:
        writer = csv.DictWriter(manifest_handle, fieldnames=MANIFEST_FIELDS)
        if source_mode == "w":
            writer.writeheader()
            manifest_handle.flush()

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            TextColumn("{task.fields[file_count]} records"),
            TextColumn("{task.fields[size]}"),
            TextColumn("{task.fields[rate]}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Starting...", total=None, file_count=f"{total_records:,}", size=format_size(total_size), rate="0.0 files/s")
            directories = [root]
            try:
                while directories:
                    directory = directories.pop()
                    try:
                        with os.scandir(directory) as entries:
                            for entry in entries:
                                path = Path(entry.path)
                                try:
                                    if entry.is_dir(follow_symlinks=False):
                                        directories.append(path)
                                        continue
                                    if not entry.is_file(follow_symlinks=False):
                                        continue
                                    if path.suffix.lower() not in ALL_KNOWN_MEDIA_EXTENSIONS:
                                        continue
                                    classification = classify_file(path)
                                    if classification is None:
                                        continue
                                    media_type, confidence = classification
                                    relative_path = path.relative_to(root).as_posix()
                                    if relative_path in existing_paths:
                                        continue
                                    stat = entry.stat(follow_symlinks=False)
                                    row = {
                                        "source_id": source["id"],
                                        "source_label": source["label"],
                                        "source_relative_path": relative_path,
                                        "filename": path.name,
                                        "extension": path.suffix.lower(),
                                        "media_type": media_type,
                                        "confidence": confidence,
                                        "size_bytes": stat.st_size,
                                        "modified_epoch": stat.st_mtime,
                                        "modified_iso": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
                                        "discovered_at": utc_now(),
                                    }
                                    batch.append(row)
                                    existing_paths.add(relative_path)
                                    total_records += 1
                                    new_records += 1
                                    total_size += stat.st_size
                                    extension_counts[row["extension"]] += 1
                                    type_counts[media_type] += 1
                                    confidence_counts[confidence] += 1
                                    current_path = relative_path
                                    if len(batch) >= MANIFEST_FLUSH_EVERY:
                                        flush_batch(manifest_handle, writer, batch)
                                        write_checkpoint(checkpoint, status="running", source_path=root, total_records=total_records, total_size=total_size, current_path=current_path, errors=errors)
                                    if new_records % PROGRESS_REFRESH_EVERY == 0:
                                        elapsed = max(time.perf_counter() - started, 0.001)
                                        progress.update(task, advance=PROGRESS_REFRESH_EVERY, description=f"Scanning {shorten_path(current_path)}", file_count=f"{total_records:,}", size=format_size(total_size), rate=f"{new_records / elapsed:.1f} files/s")
                                except Exception as error:  # per-file errors must not abort scan
                                    errors += 1
                                    with error_log.open("a", encoding="utf-8") as log_handle:
                                        log_handle.write(f"{utc_now()} | {path} | {error}\n")
                    except Exception as error:
                        errors += 1
                        with error_log.open("a", encoding="utf-8") as log_handle:
                            log_handle.write(f"{utc_now()} | DIRECTORY | {directory} | {error}\n")
            except KeyboardInterrupt:
                interrupted = True
                console.print("\n[yellow]Stopping safely and saving the current manifest batch...[/yellow]")
            finally:
                flush_batch(manifest_handle, writer, batch)
                remainder = new_records % PROGRESS_REFRESH_EVERY
                if remainder:
                    elapsed = max(time.perf_counter() - started, 0.001)
                    progress.update(task, advance=remainder, description="Finalizing manifest", file_count=f"{total_records:,}", size=format_size(total_size), rate=f"{new_records / elapsed:.1f} files/s")

    final_status = "interrupted" if interrupted else "completed"
    write_checkpoint(checkpoint, status=final_status, source_path=root, total_records=total_records, total_size=total_size, current_path=current_path, errors=errors)
    write_summary(summary, status=final_status, source=source, total_records=total_records, total_size=total_size, errors=errors, extension_counts=extension_counts, type_counts=type_counts, confidence_counts=confidence_counts)
    update_source_status(project, source["id"], final_status)

    if interrupted:
        console.print("[yellow]Scan paused safely.[/yellow] Run the same scan command later to resume.")
    else:
        console.print("[bold green]Source manifest complete.[/bold green]")
        console.print(f"Records: {total_records:,} | Selected media: {format_size(total_size)} | Errors: {errors:,}")
        console.print(f"Next: family-media index {project['slug']}")
