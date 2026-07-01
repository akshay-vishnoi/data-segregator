from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .review_pack import _read_plan
from .utils import format_size, paths_overlap, resolved

console = Console()

COPY_ACTIONS = {"planned-copy", "planned-copy-review"}
SUCCESS_STATUSES = {"copied-and-verified", "resumed-and-verified", "already-verified"}
SELECTION_FIELDS = [
    "plan_id",
    "source_id",
    "source_absolute_path",
    "source_relative_path",
    "size_bytes",
    "sha256",
    "planned_action",
    "planned_destination_relative_path",
    "media_type",
    "capture_date",
    "date_source",
]
AUDIT_FIELDS = [
    "timestamp_utc",
    "plan_id",
    "source_absolute_path",
    "planned_destination_relative_path",
    "size_bytes",
    "planned_sha256",
    "source_sha256",
    "destination_sha256",
    "status",
    "detail",
]


class FullCopyError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_dir(project_name: str) -> Path:
    path = project_dir(project_name) / "full_copy"
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


def _source_signature(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    with temporary.open("r+", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


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


def _completed_plan_ids(audit_path: Path) -> set[str]:
    if not audit_path.exists():
        return set()
    completed: set[str] = set()
    with audit_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") in SUCCESS_STATUSES and row.get("plan_id"):
                completed.add(row["plan_id"])
    return completed


def _safe_target(destination_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise FullCopyError(f"Unsafe destination path in plan: {relative_path!r}")
    candidate = destination_root / relative
    root_text = str(destination_root.resolve())
    candidate_text = str(candidate.resolve(strict=False))
    if os.path.commonpath([root_text, candidate_text]) != root_text:
        raise FullCopyError(f"Destination path escapes Family Media root: {relative_path!r}")
    return candidate


def _source_for_row(row: dict[str, str], source_roots: dict[str, Path]) -> Path:
    source_root = source_roots.get(row["source_id"])
    if source_root is None:
        raise FullCopyError(f"Plan row has an unregistered source id: {row['source_id']}")
    source = Path(row["source_absolute_path"])
    root_text = str(source_root.resolve())
    source_text = str(source.resolve(strict=False))
    if os.path.commonpath([root_text, source_text]) != root_text:
        raise FullCopyError(f"Source path escapes its registered source root: {source}")
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise FullCopyError(f"Source is unavailable, not a regular file, or is a symlink: {source}")
    if source.stat().st_size != int(row["size_bytes"]):
        raise FullCopyError(f"Source size changed since the approved plan: {source}")
    return source


def _validate_destination(project: dict, destination: str, *, same_drive_ok: bool) -> tuple[Path, dict[str, Path]]:
    destination_root = resolved(destination)
    if destination_root.name != "Family Media":
        raise FullCopyError("Destination must be the existing Family Media folder, not the drive root or source folder.")
    if not destination_root.exists() or not destination_root.is_dir() or destination_root.is_symlink():
        raise FullCopyError(f"Destination must be an existing non-symlink directory: {destination_root}")

    source_roots: dict[str, Path] = {}
    same_filesystem = False
    for source in project["sources"]:
        source_root = resolved(source["path"])
        if not source_root.exists() or not source_root.is_dir():
            raise FullCopyError(f"Registered source folder is unavailable: {source_root}")
        if paths_overlap(source_root, destination_root):
            raise FullCopyError("Destination overlaps the source folder. Refusing to copy.")
        source_roots[source["id"]] = source_root
        try:
            same_filesystem = same_filesystem or os.stat(source_root).st_dev == os.stat(destination_root).st_dev
        except OSError as error:
            raise FullCopyError(f"Could not inspect source/destination filesystem: {error}") from error

    if same_filesystem and not same_drive_ok:
        raise FullCopyError(
            "Source and destination share the same filesystem. Re-run with --same-drive-ok only after acknowledging this is organization, not backup."
        )
    return destination_root, source_roots


def _plan_rows(project_name: str) -> tuple[list[dict[str, str]], str]:
    plan_path = _plan_path(project_name)
    if not plan_path.exists():
        raise FullCopyError("Destination dry-run plan is missing. Rebuild the report-only plan first.")
    rows = [
        row
        for row in _read_plan(project_name)
        if row["planned_action"] in COPY_ACTIONS and row["planned_destination_relative_path"]
    ]
    if not rows:
        raise FullCopyError("The destination plan has no approved media-copy rows.")
    return rows, _fingerprint(plan_path)


def _select_batch(
    rows: list[dict[str, str]],
    completed_ids: set[str],
    *,
    max_files: int,
    max_total_bytes: int,
) -> tuple[list[dict[str, str]], int, int]:
    if max_files <= 0 or max_total_bytes <= 0:
        raise FullCopyError("Batch limits must be positive.")

    pending = sorted(
        [row for row in rows if row["plan_id"] not in completed_ids],
        key=lambda row: (row["plan_id"], row["source_relative_path"].casefold()),
    )
    selected: list[dict[str, str]] = []
    selected_bytes = 0
    deferred_oversize = 0
    for row in pending:
        if len(selected) >= max_files:
            break
        size = int(row["size_bytes"])
        if size > max_total_bytes:
            deferred_oversize += 1
            continue
        if selected_bytes + size > max_total_bytes:
            continue
        selected.append(row)
        selected_bytes += size
    return selected, len(pending), deferred_oversize


def _state_path(project_name: str) -> Path:
    return _runtime_dir(project_name) / "full_copy_state.json"


def _verify_or_create_state(
    project_name: str,
    *,
    plan_fingerprint: str,
    destination_root: Path,
    apply: bool,
) -> None:
    state_path = _state_path(project_name)
    if not state_path.exists():
        if not apply:
            return
        _write_json_atomic(
            state_path,
            {
                "schema_version": 1,
                "created_at": _utc_now(),
                "plan_fingerprint": plan_fingerprint,
                "destination_root": str(destination_root),
                "note": "This state pins the approved plan for resumable hash-verified migration.",
            },
        )
        return

    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FullCopyError(f"Invalid full-copy state file: {state_path}") from error
    if state.get("plan_fingerprint") != plan_fingerprint:
        raise FullCopyError(
            "The destination dry-run plan changed after full-copy state was created. Refusing to mix plans; no file was copied."
        )
    if state.get("destination_root") != str(destination_root):
        raise FullCopyError("Full-copy state belongs to a different Family Media destination. Refusing to copy.")


def _partial_path(target: Path, plan_id: str) -> Path:
    return target.with_name(f".{target.name}.data-segregator-partial-{plan_id}")


def _copy_or_resume(
    row: dict[str, str],
    *,
    destination_root: Path,
    source_roots: dict[str, Path],
) -> tuple[str, str, str]:
    source = _source_for_row(row, source_roots)
    target = _safe_target(destination_root, row["planned_destination_relative_path"])
    expected_size = int(row["size_bytes"])
    planned_hash = row["sha256"]
    before = _source_signature(source)

    if target.exists():
        if target.is_symlink() or not target.is_file() or target.stat().st_size != expected_size:
            raise FullCopyError(f"Destination already exists and is not an exact expected file: {target}")
        source_hash = _hash_file(source)
        if _source_signature(source) != before:
            raise FullCopyError(f"Source changed while being revalidated: {source}")
        if planned_hash and source_hash != planned_hash:
            raise FullCopyError(f"Source hash changed since the approved plan: {source}")
        destination_hash = _hash_file(target)
        if destination_hash != source_hash:
            raise FullCopyError(f"Destination already exists but does not match the current source: {target}")
        return "already-verified", source_hash, destination_hash

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(target, row["plan_id"])
    resumed = partial.exists()
    source_digest = hashlib.sha256()
    copied_bytes = 0

    if resumed:
        if partial.is_symlink() or not partial.is_file():
            raise FullCopyError(f"Existing partial is not a regular file: {partial}")
        partial_size = partial.stat().st_size
        if partial_size <= 0 or partial_size >= expected_size:
            raise FullCopyError(f"Existing partial has an unsafe size; inspect before retrying: {partial}")
        with source.open("rb") as source_handle, partial.open("rb") as partial_handle:
            remaining = partial_size
            while remaining:
                block = source_handle.read(min(8 * 1024 * 1024, remaining))
                partial_block = partial_handle.read(len(block))
                if not block or block != partial_block:
                    raise FullCopyError(f"Existing partial does not match the source prefix: {partial}")
                source_digest.update(block)
                copied_bytes += len(block)
                remaining -= len(block)
        write_mode = "ab"
    else:
        write_mode = "xb"

    try:
        with source.open("rb") as source_handle:
            if copied_bytes:
                source_handle.seek(copied_bytes)
            with partial.open(write_mode) as partial_handle:
                while True:
                    block = source_handle.read(8 * 1024 * 1024)
                    if not block:
                        break
                    partial_handle.write(block)
                    source_digest.update(block)
                    copied_bytes += len(block)
                partial_handle.flush()
                os.fsync(partial_handle.fileno())
    except Exception:
        raise

    if copied_bytes != expected_size or partial.stat().st_size != expected_size:
        raise FullCopyError(f"Copied size does not match the approved plan: {partial}")
    if _source_signature(source) != before:
        raise FullCopyError(f"Source changed while copying; partial is retained for inspection: {partial}")

    source_hash = source_digest.hexdigest()
    if planned_hash and source_hash != planned_hash:
        raise FullCopyError(f"Source hash changed since the approved plan; partial is retained: {partial}")
    destination_hash = _hash_file(partial)
    if destination_hash != source_hash:
        raise FullCopyError(f"Copied destination hash does not match source; partial is retained: {partial}")
    if target.exists():
        raise FullCopyError(f"Destination appeared during copy; partial is retained and no overwrite occurred: {target}")

    partial.rename(target)
    return ("resumed-and-verified" if resumed else "copied-and-verified"), source_hash, destination_hash


def _audit_row(
    row: dict[str, str],
    *,
    status: str,
    detail: str,
    source_hash: str = "",
    destination_hash: str = "",
) -> dict[str, str]:
    return {
        "timestamp_utc": _utc_now(),
        "plan_id": row["plan_id"],
        "source_absolute_path": row["source_absolute_path"],
        "planned_destination_relative_path": row["planned_destination_relative_path"],
        "size_bytes": row["size_bytes"],
        "planned_sha256": row["sha256"],
        "source_sha256": source_hash,
        "destination_sha256": destination_hash,
        "status": status,
        "detail": detail,
    }


def run_full_copy(
    project_name: str,
    *,
    destination: str,
    apply: bool,
    same_drive_ok: bool,
    confirm_count: int | None,
    max_files: int = 500,
    max_total_gb: float = 25,
) -> dict[str, int]:
    """Plan or run one bounded, resumable, hash-verified migration batch."""
    project = require_project(project_name)
    if max_total_gb <= 0:
        raise FullCopyError("--max-total-gb must be positive.")

    destination_root, source_roots = _validate_destination(project, destination, same_drive_ok=same_drive_ok)
    rows, plan_fingerprint = _plan_rows(project_name)
    runtime = _runtime_dir(project_name)
    audit_path = runtime / "full_copy_audit.csv"
    completed_ids = _completed_plan_ids(audit_path)
    batch, pending_count, deferred_oversize = _select_batch(
        rows,
        completed_ids,
        max_files=max_files,
        max_total_bytes=int(max_total_gb * 1024**3),
    )
    selection_path = runtime / "full_copy_selection.csv"
    _write_csv_atomic(selection_path, SELECTION_FIELDS, batch)

    batch_bytes = sum(int(row["size_bytes"]) for row in batch)
    total_bytes = sum(int(row["size_bytes"]) for row in rows)
    completed_bytes = sum(int(row["size_bytes"]) for row in rows if row["plan_id"] in completed_ids)
    table = Table(title="Family Media Copy — Hash-Verified Batch")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Approved plan files", f"{len(rows):,}")
    table.add_row("Approved plan size", format_size(total_bytes))
    table.add_row("Previously verified", f"{len(completed_ids):,} / {format_size(completed_bytes)}")
    table.add_row("Pending plan files", f"{pending_count:,}")
    table.add_row("This batch", f"{len(batch):,} / {format_size(batch_bytes)}")
    table.add_row("Destination", str(destination_root))
    if deferred_oversize:
        table.add_row("Deferred oversized files", f"{deferred_oversize:,}")
    console.print(table)
    console.print(f"[green]Batch selection:[/green] {selection_path}")

    if not batch:
        if pending_count:
            raise FullCopyError("No pending file fits the current batch cap. Increase --max-total-gb; no file was copied.")
        console.print("[green]No pending rows remain in this pinned plan.[/green]")
        return {"planned": len(rows), "pending": 0, "selected": 0, "copied": 0, "already_verified": 0}

    if not apply:
        console.print(
            "[yellow]Plan only. No media was copied. Re-run with --apply --confirm-count "
            + str(len(batch))
            + " to copy only this selected batch.[/yellow]"
        )
        return {"planned": len(rows), "pending": pending_count, "selected": len(batch), "copied": 0, "already_verified": 0}

    if confirm_count != len(batch):
        raise FullCopyError(
            f"Confirmation count must equal the current selected batch count ({len(batch)}). No file was copied."
        )
    _verify_or_create_state(
        project_name,
        plan_fingerprint=plan_fingerprint,
        destination_root=destination_root,
        apply=True,
    )

    copied = 0
    resumed = 0
    already_verified = 0
    for row in batch:
        try:
            status, source_hash, destination_hash = _copy_or_resume(
                row,
                destination_root=destination_root,
                source_roots=source_roots,
            )
            if status == "copied-and-verified":
                copied += 1
            elif status == "resumed-and-verified":
                resumed += 1
            else:
                already_verified += 1
            _append_audit(
                audit_path,
                _audit_row(
                    row,
                    status=status,
                    detail="Source and destination SHA-256 match; source was not modified.",
                    source_hash=source_hash,
                    destination_hash=destination_hash,
                ),
            )
            console.print(f"[green]{status}[/green] {row['planned_destination_relative_path']}")
        except Exception as error:
            _append_audit(audit_path, _audit_row(row, status="failed", detail=str(error)))
            raise FullCopyError(f"Copy stopped. The current file was not finalized, and later rows were not started. {error}") from error

    console.print(f"[green]Copy audit:[/green] {audit_path}")
    console.print("[yellow]Original source files were never changed. This remains same-drive organization, not backup.[/yellow]")
    return {
        "planned": len(rows),
        "pending": pending_count,
        "selected": len(batch),
        "copied": copied,
        "resumed": resumed,
        "already_verified": already_verified,
    }
