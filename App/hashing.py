from __future__ import annotations

import hashlib
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn

from .constants import HASH_CHUNK_BYTES
from .db import get_connection
from .project import require_project

console = Console()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def hash_exact_duplicates(project_name: str, source_label: str | None = None, limit: int | None = None) -> None:
    project = require_project(project_name)
    source_by_id = {source["id"]: source for source in project["sources"]}
    conn = get_connection(project_name)
    query = """SELECT id, source_id, source_label, source_relative_path, size_bytes
               FROM media_files WHERE sha256 IS NULL AND hash_error IS NULL
               ORDER BY size_bytes DESC"""
    params: list[object] = []
    if source_label:
        query = """SELECT id, source_id, source_label, source_relative_path, size_bytes
                   FROM media_files WHERE sha256 IS NULL AND hash_error IS NULL AND source_label=?
                   ORDER BY size_bytes DESC"""
        params.append(source_label)
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()
    if not rows:
        console.print("[green]No unhashed media matches this request.[/green]")
        conn.close()
        return

    with Progress(
        SpinnerColumn(), TextColumn("[bold magenta]Hashing for exact duplicates[/bold magenta]"), BarColumn(),
        TextColumn("{task.completed:,}/{task.total:,}"), TimeElapsedColumn(), TimeRemainingColumn(), console=console,
    ) as progress:
        task = progress.add_task("hash", total=len(rows))
        for row in rows:
            source = source_by_id.get(row["source_id"])
            if source is None:
                conn.execute("UPDATE media_files SET hash_error=? WHERE id=?", ("Source no longer registered", row["id"]))
                conn.commit()
                progress.update(task, advance=1)
                continue
            path = Path(source["path"]) / row["source_relative_path"]
            try:
                digest = sha256_file(path)
                conn.execute("UPDATE media_files SET sha256=?, hash_error=NULL WHERE id=?", (digest, row["id"]))
            except Exception as error:
                conn.execute("UPDATE media_files SET hash_error=? WHERE id=?", (str(error), row["id"]))
            conn.commit()
            progress.update(task, advance=1)
    conn.close()
    console.print("[bold green]Hash pass complete.[/bold green]")
