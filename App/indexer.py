from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .db import get_connection, init_db
from .project import require_project, source_runtime_dir

console = Console()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def line_count(path: Path) -> int:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return max(sum(1 for _ in handle) - 1, 0)


def build_index(project_name: str) -> None:
    project = require_project(project_name)
    manifests: list[tuple[dict, Path]] = []
    for source in project["sources"]:
        manifest = source_runtime_dir(project_name, source["label"]) / "manifest.csv"
        if manifest.exists():
            manifests.append((source, manifest))

    if not manifests:
        console.print("[red]No source manifests found.[/red] Run a source scan first.")
        return

    init_db(project_name)
    conn = get_connection(project_name)
    cursor = conn.cursor()
    now = utc_now()

    total = sum(line_count(manifest) for _, manifest in manifests)
    processed = 0
    batch: list[tuple] = []

    sql = """
        INSERT INTO media_files (
            source_id, source_label, source_relative_path, filename, extension,
            media_type, confidence, size_bytes, modified_epoch, modified_iso,
            discovered_at, indexed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, source_relative_path) DO UPDATE SET
            filename=excluded.filename,
            extension=excluded.extension,
            media_type=excluded.media_type,
            confidence=excluded.confidence,
            size_bytes=excluded.size_bytes,
            modified_epoch=excluded.modified_epoch,
            modified_iso=excluded.modified_iso,
            discovered_at=excluded.discovered_at,
            indexed_at=excluded.indexed_at
    """

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold green]Building catalog[/bold green]"),
        BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("index", total=total)
        for source, manifest in manifests:
            cursor.execute(
                """INSERT INTO sources(source_id, source_label, source_path, added_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(source_id) DO UPDATE SET source_label=excluded.source_label, source_path=excluded.source_path, last_seen_at=excluded.last_seen_at""",
                (source["id"], source["label"], source["path"], source["added_at"], now),
            )
            conn.commit()

            with manifest.open("r", newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    batch.append((
                        row["source_id"], row["source_label"], row["source_relative_path"],
                        row["filename"], row["extension"], row["media_type"], row["confidence"],
                        int(row["size_bytes"]), float(row["modified_epoch"]), row["modified_iso"],
                        row["discovered_at"], now,
                    ))
                    if len(batch) >= 10_000:
                        cursor.executemany(sql, batch)
                        conn.commit()
                        batch.clear()
                    processed += 1
                    progress.update(task, advance=1)

        if batch:
            cursor.executemany(sql, batch)
            conn.commit()

    conn.close()
    console.print(f"[bold green]Catalog complete.[/bold green] Rows processed: {processed:,}")
