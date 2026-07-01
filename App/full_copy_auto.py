from __future__ import annotations

import shutil

from . import full_copy as c

GIB = 1024**3
MIB = 1024**2


def _pending(rows: list[dict[str, str]], done: set[str]) -> list[dict[str, str]]:
    return sorted(
        (row for row in rows if row["plan_id"] not in done),
        key=lambda row: (row["plan_id"], row["source_relative_path"].casefold()),
    )


def _checkpoint(pending: list[dict[str, str]], files: int, total_bytes: int) -> tuple[list[dict[str, str]], bool]:
    selected, _, _ = c._select_batch(pending, set(), max_files=files, max_total_bytes=total_bytes)
    if selected:
        return selected, False
    if pending:
        return [pending[0]], True
    return [], False


def run_full_copy_automatic(
    project_name: str,
    *,
    destination: str,
    apply: bool,
    same_drive_ok: bool,
    confirm_plan_files: int | None,
    checkpoint_files: int = 500,
    checkpoint_total_gb: float = 25.0,
) -> dict[str, int]:
    """Run every approved row automatically, checkpointing and resuming safely."""
    if checkpoint_files <= 0 or checkpoint_total_gb <= 0:
        raise c.FullCopyError("Checkpoint limits must be positive.")

    project = c.require_project(project_name)
    destination_root, source_roots = c._validate_destination(project, destination, same_drive_ok=same_drive_ok)
    rows, plan_fingerprint = c._plan_rows(project_name)
    runtime = c._runtime_dir(project_name)
    audit_path = runtime / "full_copy_audit.csv"
    selection_path = runtime / "full_copy_current_checkpoint.csv"
    done = c._completed_plan_ids(audit_path)
    pending = _pending(rows, done)
    limit = int(checkpoint_total_gb * GIB)
    first, oversized = _checkpoint(pending, checkpoint_files, limit)
    c._write_csv_atomic(selection_path, c.SELECTION_FIELDS, first)

    total_bytes = sum(int(row["size_bytes"]) for row in rows)
    done_bytes = sum(int(row["size_bytes"]) for row in rows if row["plan_id"] in done)
    remaining_bytes = total_bytes - done_bytes
    c.console.print(
        f"Approved plan: {len(rows):,} files / {c.format_size(total_bytes)}\n"
        f"Previously verified: {len(done):,} / {c.format_size(done_bytes)}\n"
        f"Remaining: {len(pending):,} / {c.format_size(remaining_bytes)}\n"
        f"Automatic internal checkpoints: {checkpoint_files:,} files / {c.format_size(limit)}\n"
        f"Current checkpoint preview: {len(first):,} files / {c.format_size(sum(int(row['size_bytes']) for row in first))}\n"
        f"Destination: {destination_root}"
    )
    c.console.print(f"[green]Current checkpoint preview:[/green] {selection_path}")
    if oversized:
        c.console.print("[yellow]The first checkpoint is one oversized file and will be handled safely by itself.[/yellow]")

    if not pending:
        c.console.print("[green]No pending rows remain in this pinned plan.[/green]")
        return {"planned": len(rows), "pending": 0, "copied": 0, "resumed": 0, "already_verified": 0}
    if not apply:
        c.console.print(
            "[yellow]Plan only. No media was copied. One command with --apply --confirm-plan-files "
            + str(len(rows))
            + " will continue automatically through all remaining files.[/yellow]"
        )
        return {"planned": len(rows), "pending": len(pending), "copied": 0, "resumed": 0, "already_verified": 0}
    if confirm_plan_files != len(rows):
        raise c.FullCopyError(f"Confirmation must equal the approved plan file count ({len(rows)}). No file was copied.")

    c._verify_or_create_state(project_name, plan_fingerprint=plan_fingerprint, destination_root=destination_root, apply=True)
    minimum_free = remaining_bytes + max(10 * GIB, remaining_bytes // 20)
    if shutil.disk_usage(destination_root).free < minimum_free:
        raise c.FullCopyError("Insufficient free space for the remaining plan plus safety buffer. No file was copied.")

    copied = resumed = already_verified = checkpoint_number = 0
    while True:
        done = c._completed_plan_ids(audit_path)
        pending = _pending(rows, done)
        if not pending:
            break
        selected, oversized = _checkpoint(pending, checkpoint_files, limit)
        selected_bytes = sum(int(row["size_bytes"]) for row in selected)
        if shutil.disk_usage(destination_root).free < selected_bytes + 128 * MIB:
            raise c.FullCopyError("Insufficient free space for the next checkpoint. No new file was started.")
        c._write_csv_atomic(selection_path, c.SELECTION_FIELDS, selected)
        checkpoint_number += 1
        checkpoint_copied = checkpoint_resumed = checkpoint_existing = 0
        for row in selected:
            try:
                status, source_hash, destination_hash = c._copy_or_resume(row, destination_root=destination_root, source_roots=source_roots)
                if status == "copied-and-verified":
                    copied += 1
                    checkpoint_copied += 1
                elif status == "resumed-and-verified":
                    resumed += 1
                    checkpoint_resumed += 1
                else:
                    already_verified += 1
                    checkpoint_existing += 1
                c._append_audit(audit_path, c._audit_row(row, status=status, detail="Source and destination SHA-256 match; source was not modified.", source_hash=source_hash, destination_hash=destination_hash))
            except Exception as error:
                c._append_audit(audit_path, c._audit_row(row, status="failed", detail=str(error)))
                raise c.FullCopyError("Copy stopped safely. Later rows were not started; rerun this same command to resume verified work. " + str(error)) from error
        done = c._completed_plan_ids(audit_path)
        done_bytes = sum(int(row["size_bytes"]) for row in rows if row["plan_id"] in done)
        suffix = " (single oversized file)" if oversized else ""
        c.console.print(
            f"[green]Checkpoint {checkpoint_number} complete{suffix}:[/green] "
            f"+{checkpoint_copied} copied, +{checkpoint_resumed} resumed, +{checkpoint_existing} already verified. "
            f"Overall {len(done):,}/{len(rows):,} files / {c.format_size(done_bytes)}."
        )

    c.console.print(f"[green]Copy audit:[/green] {audit_path}")
    c.console.print("[green]Approved media plan completed with source/destination SHA-256 verification.[/green]")
    c.console.print("[yellow]Original source files were never changed. This remains same-drive organization, not backup.[/yellow]")
    return {"planned": len(rows), "pending": 0, "copied": copied, "resumed": resumed, "already_verified": already_verified}
