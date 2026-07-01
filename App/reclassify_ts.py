from __future__ import annotations

import csv
import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.table import Table

from .classify import classify_file
from .constants import MANIFEST_FIELDS
from .db import database_path, get_connection
from .project import get_source, source_runtime_dir
from .scanner import load_manifest, write_checkpoint, write_summary
from .utils import format_size

console = Console()


@dataclass(frozen=True)
class TsReclassificationPlan:
    project_name: str
    source_label: str
    manifest_path: Path
    ts_records: int
    retain_candidate_media: int
    retain_unverified: int
    remove_nonmedia: int
    remove_bytes: int
    changed_since_scan: int


def _utc_file_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _row_matches_current_file(row: dict[str, str], path: Path) -> bool:
    try:
        stat = path.stat()
        expected_size = int(row["size_bytes"])
        expected_mtime = float(row["modified_epoch"])
    except (OSError, ValueError, KeyError):
        return False
    return stat.st_size == expected_size and abs(stat.st_mtime - expected_mtime) < 0.01


def _chunked(values: Iterable[str], size: int) -> Iterable[list[str]]:
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _read_manifest_rows(manifest_path: Path) -> list[dict[str, str]]:
    with manifest_path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_ts_reclassification_plan(project_name: str, source_label: str) -> tuple[TsReclassificationPlan, list[dict[str, str]]]:
    _, source = get_source(project_name, source_label)
    runtime = source_runtime_dir(project_name, source_label)
    manifest_path = runtime / "manifest.csv"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest found for source '{source_label}'. Run a scan first.")

    root = Path(source["path"])
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Source '{source_label}' is unavailable at {root}. Use source relink first.")

    rows = _read_manifest_rows(manifest_path)
    ts_records = 0
    retained_candidates = 0
    retained_unverified = 0
    remove_nonmedia = 0
    remove_bytes = 0
    changed_since_scan = 0

    for row in rows:
        if row.get("extension", "").lower() != ".ts":
            continue
        ts_records += 1
        path = root / row["source_relative_path"]

        if not path.exists() or not _row_matches_current_file(row, path):
            retained_unverified += 1
            changed_since_scan += 1
            continue

        if classify_file(path) is None:
            remove_nonmedia += 1
            remove_bytes += int(row["size_bytes"])
        else:
            retained_candidates += 1

    plan = TsReclassificationPlan(
        project_name=project_name,
        source_label=source_label,
        manifest_path=manifest_path,
        ts_records=ts_records,
        retain_candidate_media=retained_candidates,
        retain_unverified=retained_unverified,
        remove_nonmedia=remove_nonmedia,
        remove_bytes=remove_bytes,
        changed_since_scan=changed_since_scan,
    )
    return plan, rows


def show_ts_reclassification_plan(plan: TsReclassificationPlan) -> None:
    table = Table(title=".ts Reclassification Plan")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Current .ts records", f"{plan.ts_records:,}")
    table.add_row("Retained as candidate MPEG TS", f"{plan.retain_candidate_media:,}")
    table.add_row("Retained because file changed/unavailable", f"{plan.retain_unverified:,}")
    table.add_row("Confirmed non-media to remove", f"{plan.remove_nonmedia:,}")
    table.add_row("Non-media bytes to remove", format_size(plan.remove_bytes))
    table.add_row("Changed since scan", f"{plan.changed_since_scan:,}")
    console.print(table)
    console.print("[yellow]Plan only: no manifest or database changes have been made.[/yellow]")


def _write_manifest_atomically(manifest_path: Path, rows: list[dict[str, str]]) -> Path:
    backup = manifest_path.with_name(f"manifest.before-ts-reclassification-{_utc_file_stamp()}.csv")
    shutil.copy2(manifest_path, backup)

    temporary = manifest_path.with_suffix(".reclassifying.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(manifest_path)
    return backup


def _delete_catalog_rows(project_name: str, source_id: str, relative_paths: list[str]) -> int:
    if not relative_paths or not database_path(project_name).exists():
        return 0
    conn = get_connection(project_name)
    deleted = 0
    try:
        for chunk in _chunked(relative_paths, 500):
            placeholders = ",".join("?" for _ in chunk)
            cursor = conn.execute(
                f"""DELETE FROM media_files
                    WHERE source_id=? AND extension='.ts'
                      AND source_relative_path IN ({placeholders})""",
                [source_id, *chunk],
            )
            deleted += cursor.rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted


def _previous_error_count(runtime: Path) -> int:
    checkpoint = runtime / "checkpoint.json"
    if not checkpoint.exists():
        return 0
    try:
        return int(json.loads(checkpoint.read_text(encoding="utf-8")).get("errors", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def apply_ts_reclassification(project_name: str, source_label: str) -> tuple[TsReclassificationPlan, Path, int]:
    plan, original_rows = build_ts_reclassification_plan(project_name, source_label)
    _, source = get_source(project_name, source_label)
    runtime = source_runtime_dir(project_name, source_label)

    remove_paths: list[str] = []
    removed_bytes = 0
    retained_rows: list[dict[str, str]] = []
    root = Path(source["path"])
    for row in original_rows:
        if row.get("extension", "").lower() != ".ts":
            retained_rows.append(row)
            continue
        path = root / row["source_relative_path"]
        if _row_matches_current_file(row, path) and classify_file(path) is None:
            remove_paths.append(row["source_relative_path"])
            removed_bytes += int(row["size_bytes"])
            continue
        retained_rows.append(row)

    # A second inspection protects against a source changing between planning and
    # application. A changed or unavailable file is retained rather than removed.
    backup_path = _write_manifest_atomically(plan.manifest_path, retained_rows)
    deleted_catalog_rows = _delete_catalog_rows(project_name, source["id"], remove_paths)

    _, extension_counts, type_counts, confidence_counts, total_size, total_records = load_manifest(plan.manifest_path)
    errors = _previous_error_count(runtime)
    write_checkpoint(
        runtime / "checkpoint.json",
        status="completed",
        source_path=Path(source["path"]),
        total_records=total_records,
        total_size=total_size,
        current_path=".ts reclassification applied",
        errors=errors,
    )
    write_summary(
        runtime / "scan_summary.md",
        status="completed",
        source=source,
        total_records=total_records,
        total_size=total_size,
        errors=errors,
        extension_counts=extension_counts,
        type_counts=type_counts,
        confidence_counts=confidence_counts,
    )

    audit_path = runtime / "ts_reclassification.md"
    audit_path.write_text(
        "\n".join([
            "# .ts Reclassification", "",
            f"Applied: `{datetime.now(timezone.utc).isoformat()}`", "",
            f"- Source: `{source['label']}`",
            f"- Original .ts records: **{plan.ts_records:,}**",
            f"- Confirmed non-media records removed from media manifest: **{len(remove_paths):,}**",
            f"- Removed bytes: **{format_size(removed_bytes)}**",
            f"- Retained candidate MPEG transport streams: **{plan.retain_candidate_media:,}**",
            f"- Retained due to file change/unavailability: **{plan.retain_unverified:,}**",
            f"- Catalog rows removed: **{deleted_catalog_rows:,}**",
            f"- Manifest backup: `{backup_path.name}`", "",
            "No source file was changed. Existing duplicate and canonical reports should be regenerated.",
        ]) + "\n",
        encoding="utf-8",
    )
    return plan, backup_path, deleted_catalog_rows
