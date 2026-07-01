from __future__ import annotations

import csv
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .utils import format_size, resolved

console = Console()
SAFE_STATUS = "safe-cleanup-candidate"
AUDIT_FIELDS = [
    "timestamp_utc",
    "plan_id",
    "destination_relative_path",
    "destination_absolute_path",
    "size_bytes",
    "expected_sha256",
    "status",
    "detail",
]


class PilotCleanupError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise PilotCleanupError(f"Required file is missing: {path}")
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _append_audit(path: Path, row: dict[str, str]) -> None:
    create_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        if create_header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def _safe_target(destination_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise PilotCleanupError(f"Unsafe destination path: {relative_path!r}")
    candidate = destination_root / relative
    root_text = str(destination_root.resolve())
    candidate_text = str(candidate.resolve(strict=False))
    if os.path.commonpath([root_text, candidate_text]) != root_text:
        raise PilotCleanupError(f"Cleanup target escapes Family Media: {relative_path!r}")
    return candidate


def _audit_row(row: dict[str, str], target: Path, status: str, detail: str) -> dict[str, str]:
    return {
        "timestamp_utc": _utc_now(),
        "plan_id": row["plan_id"],
        "destination_relative_path": row["destination_relative_path"],
        "destination_absolute_path": str(target),
        "size_bytes": row["size_bytes"],
        "expected_sha256": row["expected_sha256"],
        "status": status,
        "detail": detail,
    }


def _eligible_rows(project_name: str, destination_root: Path) -> list[dict[str, str]]:
    runtime = project_dir(project_name)
    cleanup_rows = _read_csv(runtime / "pilot_copy" / "pilot_cleanup_plan.csv")
    current_plan_rows = _read_csv(runtime / "plans" / "destination_dry_run_plan.csv")
    current_by_id = {row["plan_id"]: row for row in current_plan_rows if row.get("plan_id")}

    candidates = [row for row in cleanup_rows if row.get("cleanup_status") == SAFE_STATUS]
    if not candidates:
        raise PilotCleanupError("No safe cleanup candidates are present. Regenerate the cleanup plan first.")

    for row in candidates:
        current = current_by_id.get(row["plan_id"])
        if current is None:
            raise PilotCleanupError(f"Current destination plan no longer contains pilot record: {row['plan_id']}")
        if current.get("planned_action") != "preserve-exclude-policy":
            raise PilotCleanupError(
                "Current policy no longer excludes this pilot file; refusing cleanup: "
                + row["destination_relative_path"]
            )
        target = _safe_target(destination_root, row["destination_relative_path"])
        if str(target) != row["destination_absolute_path"]:
            raise PilotCleanupError("Cleanup-plan destination does not match the current Family Media root.")
    return candidates


def run_pilot_cleanup(
    project_name: str,
    *,
    destination: str,
    apply: bool,
    confirm_count: int | None,
) -> dict[str, int]:
    """Remove only verified destination pilot copies that the current policy excludes."""
    require_project(project_name)
    destination_root = resolved(destination)
    if destination_root.name != "Family Media" or not destination_root.exists() or not destination_root.is_dir():
        raise PilotCleanupError("Destination must be the existing Family Media folder.")

    candidates = _eligible_rows(project_name, destination_root)
    total_bytes = sum(int(row["size_bytes"]) for row in candidates)
    table = Table(title="Pilot Cleanup — Destination Copies Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Verified cleanup candidates", f"{len(candidates):,}")
    table.add_row("Cleanup candidate size", format_size(total_bytes))
    table.add_row("Destination", str(destination_root))
    console.print(table)

    if not apply:
        console.print("[yellow]Plan only. No file was removed. Re-run with --apply --confirm-count " + str(len(candidates)) + " to remove only these verified destination copies.[/yellow]")
        return {"candidates": len(candidates), "deleted": 0, "deleted_bytes": 0}

    if confirm_count != len(candidates):
        raise PilotCleanupError(
            f"Confirmation count must equal the current verified candidate count ({len(candidates)}). No file was removed."
        )

    audit_path = project_dir(project_name) / "pilot_copy" / "pilot_cleanup_audit.csv"
    deleted = 0
    deleted_bytes = 0
    for row in candidates:
        target = _safe_target(destination_root, row["destination_relative_path"])
        try:
            if not target.exists() or not target.is_file() or target.is_symlink():
                raise PilotCleanupError("Destination target is missing, not a regular file, or is a symlink.")
            expected_size = int(row["size_bytes"])
            if target.stat().st_size != expected_size:
                raise PilotCleanupError("Destination size no longer matches the verified cleanup plan.")
            current_hash = _hash_file(target)
            if current_hash != row["expected_sha256"]:
                raise PilotCleanupError("Destination hash no longer matches the verified cleanup plan.")
            target.unlink()
            deleted += 1
            deleted_bytes += expected_size
            _append_audit(audit_path, _audit_row(row, target, "deleted-verified-destination-copy", "Deleted only after current size/hash/policy verification."))
            console.print(f"[green]deleted verified destination copy[/green] {row['destination_relative_path']}")
        except Exception as error:
            _append_audit(audit_path, _audit_row(row, target, "failed", str(error)))
            raise PilotCleanupError(f"Cleanup stopped. Remaining candidates were not removed. {error}") from error

    console.print(f"[green]Cleanup audit:[/green] {audit_path}")
    console.print("[yellow]Original source files were never changed.[/yellow]")
    return {"candidates": len(candidates), "deleted": deleted, "deleted_bytes": deleted_bytes}
