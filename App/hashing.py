from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn, TimeRemainingColumn
from rich.table import Table

from .constants import HASH_CHUNK_BYTES, HASH_COMMIT_EVERY
from .db import get_connection
from .project import project_dir, require_project
from .utils import format_size

console = Console()


@dataclass(frozen=True)
class HashPlan:
    project_name: str
    source_label: str | None
    total_records: int
    hashed_records: int
    hash_errors: int
    pending_records: int
    pending_bytes: int
    candidate_records: int
    candidate_bytes: int
    candidate_size_groups: int
    skipped_single_size_records: int
    skipped_single_size_bytes: int
    breakdown: tuple[tuple[str, str, int, int], ...]


_DUPLICATE_SIZE_CTE = """
WITH duplicate_sizes AS (
    SELECT size_bytes
    FROM media_files
    GROUP BY size_bytes
    HAVING COUNT(*) > 1
)
"""


def _scope_clause(source_label: str | None) -> tuple[str, list[object]]:
    if source_label:
        return " AND source_label=?", [source_label]
    return "", []


def sha256_file(path: Path) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError("File changed while hashing; no hash was saved")
    return digest.hexdigest()


def build_hash_plan(project_name: str, source_label: str | None = None) -> HashPlan:
    require_project(project_name)
    scope_sql, scope_params = _scope_clause(source_label)
    conn = get_connection(project_name)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) AS count FROM media_files WHERE 1=1{scope_sql}",
            scope_params,
        ).fetchone()
        hashed = conn.execute(
            f"SELECT COUNT(*) AS count FROM media_files WHERE sha256 IS NOT NULL{scope_sql}",
            scope_params,
        ).fetchone()
        errors = conn.execute(
            f"SELECT COUNT(*) AS count FROM media_files WHERE hash_error IS NOT NULL{scope_sql}",
            scope_params,
        ).fetchone()
        pending = conn.execute(
            f"""SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes
                 FROM media_files
                 WHERE sha256 IS NULL AND hash_error IS NULL{scope_sql}""",
            scope_params,
        ).fetchone()
        candidate = conn.execute(
            _DUPLICATE_SIZE_CTE
            + f"""SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes,
                        COUNT(DISTINCT size_bytes) AS size_groups
                 FROM media_files
                 WHERE sha256 IS NULL AND hash_error IS NULL{scope_sql}
                   AND size_bytes IN (SELECT size_bytes FROM duplicate_sizes)""",
            scope_params,
        ).fetchone()
        breakdown_rows = conn.execute(
            _DUPLICATE_SIZE_CTE
            + f"""SELECT media_type, confidence, COUNT(*) AS count,
                        COALESCE(SUM(size_bytes), 0) AS bytes
                 FROM media_files
                 WHERE sha256 IS NULL AND hash_error IS NULL{scope_sql}
                   AND size_bytes IN (SELECT size_bytes FROM duplicate_sizes)
                 GROUP BY media_type, confidence
                 ORDER BY bytes DESC, count DESC""",
            scope_params,
        ).fetchall()
    finally:
        conn.close()

    candidate_records = int(candidate["count"])
    candidate_bytes = int(candidate["bytes"])
    pending_records = int(pending["count"])
    pending_bytes = int(pending["bytes"])
    return HashPlan(
        project_name=project_name,
        source_label=source_label,
        total_records=int(total["count"]),
        hashed_records=int(hashed["count"]),
        hash_errors=int(errors["count"]),
        pending_records=pending_records,
        pending_bytes=pending_bytes,
        candidate_records=candidate_records,
        candidate_bytes=candidate_bytes,
        candidate_size_groups=int(candidate["size_groups"]),
        skipped_single_size_records=pending_records - candidate_records,
        skipped_single_size_bytes=pending_bytes - candidate_bytes,
        breakdown=tuple(
            (str(row["media_type"]), str(row["confidence"]), int(row["count"]), int(row["bytes"]))
            for row in breakdown_rows
        ),
    )


def generate_hash_plan_report(project_name: str, source_label: str | None = None) -> tuple[HashPlan, Path]:
    plan = build_hash_plan(project_name, source_label)
    report_dir = project_dir(project_name) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    output = report_dir / "exact_duplicate_hash_plan.md"
    scope = f"Source: `{source_label}`" if source_label else "Scope: all registered sources"
    lines = [
        "# Exact Duplicate Hash Plan", "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`", scope, "",
        "## Planned reads", "",
        f"- Candidate files to hash: **{plan.candidate_records:,}**",
        f"- Candidate bytes to read: **{format_size(plan.candidate_bytes)}**",
        f"- Same-size groups represented: **{plan.candidate_size_groups:,}**", "",
        "## Skipped without reading", "",
        f"- Unhashed files with a unique size: **{plan.skipped_single_size_records:,}**",
        f"- Bytes avoided: **{format_size(plan.skipped_single_size_bytes)}**", "",
        "## Catalog state", "",
        f"- Catalog records in scope: **{plan.total_records:,}**",
        f"- Already hashed: **{plan.hashed_records:,}**",
        f"- Existing hash errors: **{plan.hash_errors:,}**",
        f"- Unhashed records in scope: **{plan.pending_records:,}** ({format_size(plan.pending_bytes)})", "",
        "## Candidate breakdown", "",
        "| Type | Confidence | Files to hash | Bytes to read |",
        "|---|---|---:|---:|",
    ]
    lines.extend(
        f"| {media_type} | {confidence} | {count:,} | {format_size(size_bytes)} |"
        for media_type, confidence, count, size_bytes in plan.breakdown
    )
    lines.extend([
        "", "## Safety notes", "",
        "- Exact duplicates must have the same byte size. Files whose size occurs only once cannot be exact duplicates, so they are skipped without being read.",
        "- The plan considers matching sizes across the entire project, even when hashing one specific source.",
        "- Hashing reads source files only. It never moves, renames, deletes, or modifies a source file.",
        "- Completed hashes are saved in batches and reused on the next run, so a stopped hash pass resumes from saved progress.",
    ])
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return plan, output


def show_hash_plan(plan: HashPlan, report_path: Path) -> None:
    table = Table(title="Exact Duplicate Hash Plan")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Files to hash", f"{plan.candidate_records:,}")
    table.add_row("Bytes to read", format_size(plan.candidate_bytes))
    table.add_row("Same-size groups", f"{plan.candidate_size_groups:,}")
    table.add_row("Skipped unique-size files", f"{plan.skipped_single_size_records:,}")
    table.add_row("Bytes avoided", format_size(plan.skipped_single_size_bytes))
    table.add_row("Already hashed", f"{plan.hashed_records:,}")
    table.add_row("Hash errors", f"{plan.hash_errors:,}")
    console.print(table)
    console.print(f"[green]Hash plan report created:[/green] {report_path}")


def _candidate_hash_query(source_label: str | None, limit: int | None) -> tuple[str, list[object]]:
    scope_sql, params = _scope_clause(source_label)
    query = _DUPLICATE_SIZE_CTE + f"""
        SELECT id, source_id, source_label, source_relative_path, size_bytes
        FROM media_files
        WHERE sha256 IS NULL AND hash_error IS NULL{scope_sql}
          AND size_bytes IN (SELECT size_bytes FROM duplicate_sizes)
        ORDER BY size_bytes DESC, id
    """
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)
    return query, params


def hash_exact_duplicates(project_name: str, source_label: str | None = None, limit: int | None = None) -> None:
    project = require_project(project_name)
    plan, report_path = generate_hash_plan_report(project_name, source_label)
    show_hash_plan(plan, report_path)
    if limit == 0:
        console.print("[green]Plan only. No source file contents were read.[/green]")
        return

    total = min(plan.candidate_records, limit) if limit is not None else plan.candidate_records
    if total == 0:
        console.print("[green]No same-size unhashed files need exact-duplicate hashing.[/green]")
        return

    source_by_id = {source["id"]: source for source in project["sources"]}
    conn = get_connection(project_name)
    query, params = _candidate_hash_query(source_label, limit)
    cursor = conn.execute(query, params)
    dirty_updates = 0
    interrupted = False
    try:
        with Progress(
            SpinnerColumn(), TextColumn("[bold magenta]Hashing exact-duplicate candidates[/bold magenta]"), BarColumn(),
            TextColumn("{task.completed:,}/{task.total:,}"), TimeElapsedColumn(), TimeRemainingColumn(), console=console,
        ) as progress:
            task = progress.add_task("hash", total=total)
            for row in cursor:
                source = source_by_id.get(row["source_id"])
                if source is None:
                    conn.execute("UPDATE media_files SET hash_error=? WHERE id=?", ("Source no longer registered", row["id"]))
                else:
                    path = Path(source["path"]) / row["source_relative_path"]
                    try:
                        digest = sha256_file(path)
                        conn.execute("UPDATE media_files SET sha256=?, hash_error=NULL WHERE id=?", (digest, row["id"]))
                    except Exception as error:
                        conn.execute("UPDATE media_files SET hash_error=? WHERE id=?", (str(error), row["id"]))
                dirty_updates += 1
                if dirty_updates >= HASH_COMMIT_EVERY:
                    conn.commit()
                    dirty_updates = 0
                progress.update(task, advance=1)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        if dirty_updates:
            conn.commit()
        conn.close()

    if interrupted:
        console.print("[yellow]Hash pass stopped. Saved work is kept; rerun the same command to continue.[/yellow]")
        return
    console.print("[bold green]Hash pass complete.[/bold green]")
