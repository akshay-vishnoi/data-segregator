from __future__ import annotations

import csv
import hashlib
import os
import shutil
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .review_pack import _read_plan
from .utils import format_size, paths_overlap, resolved

console = Console()

COPY_ACTIONS = {"planned-copy", "planned-copy-review"}
DEFAULT_MAX_FILE_BYTES = 1024**3
DEFAULT_MAX_TOTAL_BYTES = 8 * 1024**3

SELECTION_FIELDS = [
    "pilot_rank",
    "pilot_bucket",
    "plan_fingerprint",
    "plan_id",
    "source_id",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "media_type",
    "classification_confidence",
    "size_bytes",
    "sha256",
    "capture_date",
    "date_source",
    "planned_action",
    "planned_destination_relative_path",
]

AUDIT_FIELDS = [
    "timestamp_utc",
    "plan_id",
    "pilot_bucket",
    "source_absolute_path",
    "planned_destination_relative_path",
    "size_bytes",
    "expected_sha256",
    "status",
    "detail",
]


class PilotCopyError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pilot_dir(project_name: str) -> Path:
    path = project_dir(project_name) / "pilot_copy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _plan_path(project_name: str) -> Path:
    return project_dir(project_name) / "plans" / "destination_dry_run_plan.csv"


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_csv_atomic(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _append_audit(path: Path, row: dict[str, str]) -> None:
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def _stable_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(
        rows,
        key=lambda row: hashlib.sha256(row["plan_id"].encode("utf-8")).hexdigest(),
    )


def _bucket_specs() -> list[tuple[str, int, Callable[[dict[str, str]], bool]]]:
    return [
        (
            "date-library-photos",
            8,
            lambda row: row["media_type"] == "photo"
            and row["planned_destination_relative_path"].startswith("Photos/"),
        ),
        (
            "date-library-videos",
            8,
            lambda row: row["media_type"] == "video"
            and row["planned_destination_relative_path"].startswith("Videos/"),
        ),
        (
            "needs-date-review",
            4,
            lambda row: row["planned_destination_relative_path"].startswith("Needs Date Review/"),
        ),
        (
            "unknown-date",
            2,
            lambda row: row["planned_destination_relative_path"].startswith("Unknown Date/"),
        ),
        (
            "candidate-media",
            2,
            lambda row: row["classification_confidence"] != "definite",
        ),
    ]


def _select_rows(
    rows: list[dict[str, str]],
    *,
    max_file_bytes: int,
    max_total_bytes: int,
) -> list[dict[str, str]]:
    eligible = [
        row
        for row in rows
        if row["planned_action"] in COPY_ACTIONS
        and row["sha256"]
        and int(row["size_bytes"]) > 0
        and int(row["size_bytes"]) <= max_file_bytes
    ]
    selected: list[dict[str, str]] = []
    selected_ids: set[str] = set()
    total_bytes = 0

    for bucket, limit, predicate in _bucket_specs():
        chosen = 0
        for row in _stable_rows([item for item in eligible if predicate(item)]):
            if chosen >= limit:
                break
            if row["plan_id"] in selected_ids:
                continue
            size = int(row["size_bytes"])
            if total_bytes + size > max_total_bytes:
                continue
            selected.append({"pilot_bucket": bucket, **row})
            selected_ids.add(row["plan_id"])
            total_bytes += size
            chosen += 1

    return selected


def _safe_target(destination_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise PilotCopyError(f"Unsafe destination path in plan: {relative_path!r}")
    candidate = destination_root / relative
    root_text = str(destination_root.resolve())
    candidate_text = str(candidate.resolve(strict=False))
    if os.path.commonpath([root_text, candidate_text]) != root_text:
        raise PilotCopyError(f"Destination escapes Family Media root: {relative_path!r}")
    return candidate


def _validate_destination(project: dict, destination: str, *, same_drive_ok: bool) -> Path:
    destination_root = resolved(destination)
    if destination_root.name != "Family Media":
        raise PilotCopyError("Destination must be the existing Family Media folder, not the drive root or source folder.")
    if not destination_root.exists() or not destination_root.is_dir():
        raise PilotCopyError(f"Destination folder does not exist: {destination_root}")

    same_filesystem = False
    for source in project["sources"]:
        source_root = resolved(source["path"])
        if paths_overlap(source_root, destination_root):
            raise PilotCopyError("Destination overlaps the source folder. Refusing to copy.")
        try:
            same_filesystem = same_filesystem or os.stat(source_root).st_dev == os.stat(destination_root).st_dev
        except OSError as error:
            raise PilotCopyError(f"Could not inspect source/destination filesystem: {error}") from error

    if same_filesystem and not same_drive_ok:
        raise PilotCopyError(
            "Source and destination are on the same physical filesystem. Re-run with --same-drive-ok only after acknowledging this is organization, not backup."
        )
    return destination_root


def _selection_rows(project_name: str, *, max_file_bytes: int, max_total_bytes: int) -> tuple[list[dict[str, str]], str]:
    plan = _plan_path(project_name)
    if not plan.exists():
        raise PilotCopyError("Destination dry-run plan is missing. Build it before the pilot.")
    plan_fingerprint = _fingerprint(plan)
    selected = _select_rows(
        _read_plan(project_name),
        max_file_bytes=max_file_bytes,
        max_total_bytes=max_total_bytes,
    )
    if not selected:
        raise PilotCopyError("No hash-verified pilot candidates fit the pilot limits.")

    selection = []
    for rank, row in enumerate(selected, start=1):
        selection.append({
            "pilot_rank": str(rank),
            "plan_fingerprint": plan_fingerprint,
            **row,
        })
    return selection, plan_fingerprint


def _audit_row(row: dict[str, str], status: str, detail: str) -> dict[str, str]:
    return {
        "timestamp_utc": _utc_now(),
        "plan_id": row["plan_id"],
        "pilot_bucket": row["pilot_bucket"],
        "source_absolute_path": row["source_absolute_path"],
        "planned_destination_relative_path": row["planned_destination_relative_path"],
        "size_bytes": row["size_bytes"],
        "expected_sha256": row["sha256"],
        "status": status,
        "detail": detail,
    }


def _copy_one(row: dict[str, str], destination_root: Path) -> str:
    source = Path(row["source_absolute_path"])
    if not source.exists() or not source.is_file():
        raise PilotCopyError(f"Source file is unavailable: {source}")
    source_size = source.stat().st_size
    expected_size = int(row["size_bytes"])
    if source_size != expected_size:
        raise PilotCopyError(f"Source size changed since planning: {source}")

    expected_hash = row["sha256"]
    source_hash = _hash_file(source)
    if source_hash != expected_hash:
        raise PilotCopyError(f"Source hash changed since planning: {source}")

    target = _safe_target(destination_root, row["planned_destination_relative_path"])
    if target.exists():
        if target.is_file() and target.stat().st_size == expected_size and _hash_file(target) == expected_hash:
            return "already-verified"
        raise PilotCopyError(f"Destination already exists and is not an exact verified match: {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f".{target.name}.pilot-partial-{row['plan_id']}")
    if partial.exists():
        raise PilotCopyError(f"An earlier unfinished pilot file exists; inspect it before retrying: {partial}")

    try:
        with source.open("rb") as source_handle, partial.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=8 * 1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        if partial.stat().st_size != expected_size:
            raise PilotCopyError(f"Copied size does not match source: {partial}")
        if _hash_file(partial) != expected_hash:
            raise PilotCopyError(f"Copied hash does not match source: {partial}")
        if target.exists():
            raise PilotCopyError(f"Destination appeared during copy; partial retained: {partial}")
        partial.rename(target)
        if _hash_file(target) != expected_hash:
            raise PilotCopyError(f"Final destination hash does not match source: {target}")
    except Exception:
        raise
    return "copied-and-verified"


def run_pilot_copy(
    project_name: str,
    *,
    destination: str,
    apply: bool,
    same_drive_ok: bool,
    max_file_mb: int = 1024,
    max_total_gb: int = 8,
) -> dict[str, int]:
    """Build, or explicitly apply, a small hash-verified pilot copy from an approved dry-run plan."""
    project = require_project(project_name)
    if max_file_mb <= 0 or max_total_gb <= 0:
        raise PilotCopyError("Pilot limits must be positive.")

    destination_root = _validate_destination(project, destination, same_drive_ok=same_drive_ok)
    selection, _ = _selection_rows(
        project_name,
        max_file_bytes=max_file_mb * 1024**2,
        max_total_bytes=max_total_gb * 1024**3,
    )
    pilot_dir = _pilot_dir(project_name)
    selection_path = pilot_dir / "pilot_selection.csv"
    audit_path = pilot_dir / "pilot_copy_audit.csv"
    _write_csv_atomic(selection_path, SELECTION_FIELDS, selection)

    total_bytes = sum(int(row["size_bytes"]) for row in selection)
    table = Table(title="Pilot Copy — Hash-Verified")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Selected files", f"{len(selection):,}")
    table.add_row("Pilot size", format_size(total_bytes))
    table.add_row("Destination", str(destination_root))
    table.add_row("Same-drive organization acknowledged", "yes" if same_drive_ok else "no")
    console.print(table)
    console.print(f"[green]Pilot selection:[/green] {selection_path}")

    if not apply:
        console.print("[yellow]Plan only. No media files were copied. Re-run with --apply to start the pilot.[/yellow]")
        return {"selected": len(selection), "selected_bytes": total_bytes, "copied": 0, "already_verified": 0}

    copied = 0
    already_verified = 0
    for row in selection:
        try:
            status = _copy_one(row, destination_root)
            if status == "copied-and-verified":
                copied += 1
            else:
                already_verified += 1
            _append_audit(audit_path, _audit_row(row, status, "source and destination SHA-256 match"))
            console.print(f"[green]{status}[/green] {row['planned_destination_relative_path']}")
        except Exception as error:
            _append_audit(audit_path, _audit_row(row, "failed", str(error)))
            raise PilotCopyError(f"Pilot stopped. No overwrite was attempted. {error}") from error

    console.print(f"[green]Pilot audit:[/green] {audit_path}")
    console.print("[yellow]Original source files were not changed. This remains same-drive organization, not backup.[/yellow]")
    return {
        "selected": len(selection),
        "selected_bytes": total_bytes,
        "copied": copied,
        "already_verified": already_verified,
    }
