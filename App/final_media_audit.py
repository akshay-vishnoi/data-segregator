from __future__ import annotations

import csv
import hashlib
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .review_pack import _read_plan
from .utils import format_size, resolved

console = Console()
COPY_ACTIONS = {"planned-copy", "planned-copy-review"}
SUCCESS_STATUSES = {"copied-and-verified", "resumed-and-verified", "already-verified"}
REPORT_FIELDS = [
    "plan_id",
    "destination_relative_path",
    "destination_absolute_path",
    "expected_size_bytes",
    "actual_size_bytes",
    "expected_sha256",
    "actual_sha256",
    "copy_audit_record",
    "audit_status",
    "detail",
]


class FinalMediaAuditError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runtime_dir(project_name: str) -> Path:
    path = project_dir(project_name) / "final_audit"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_csv_atomic(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _safe_target(destination_root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise FinalMediaAuditError(f"Unsafe destination path in approved plan: {relative_path!r}")
    candidate = destination_root / relative
    root_text = str(destination_root.resolve())
    candidate_text = str(candidate.resolve(strict=False))
    if os.path.commonpath([root_text, candidate_text]) != root_text:
        raise FinalMediaAuditError(f"Approved destination escapes Family Media root: {relative_path!r}")
    return candidate


def _planned_rows(project_name: str) -> list[dict[str, str]]:
    rows = [
        row
        for row in _read_plan(project_name)
        if row["planned_action"] in COPY_ACTIONS and row["planned_destination_relative_path"]
    ]
    if not rows:
        raise FinalMediaAuditError("No approved media-copy rows are present in the current destination plan.")
    return rows


def _successful_copy_ids(project_name: str) -> set[str]:
    audit_path = project_dir(project_name) / "full_copy" / "full_copy_audit.csv"
    if not audit_path.exists():
        raise FinalMediaAuditError(f"Full-copy audit is missing: {audit_path}")
    successful: set[str] = set()
    with audit_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") in SUCCESS_STATUSES and row.get("plan_id"):
                successful.add(row["plan_id"])
    return successful


def _plan_state_status(project_name: str) -> tuple[str, str]:
    state_path = project_dir(project_name) / "full_copy" / "full_copy_state.json"
    plan_path = project_dir(project_name) / "plans" / "destination_dry_run_plan.csv"
    if not state_path.exists():
        return "missing", "Full-copy state file is missing. Destination files can still be audited against the current plan."
    if not plan_path.exists():
        return "unavailable", "Current destination plan is missing."
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "invalid", "Full-copy state JSON cannot be read."
    digest = _hash_file(plan_path)
    if state.get("plan_fingerprint") == digest:
        return "matched", "Current destination plan matches the migration-pinned plan fingerprint."
    return "changed", "Current destination plan does not match the migration-pinned plan fingerprint."


def _report_row(
    row: dict[str, str],
    *,
    target: Path,
    actual_size: str,
    actual_hash: str,
    copy_audit_record: str,
    status: str,
    detail: str,
) -> dict[str, str]:
    return {
        "plan_id": row["plan_id"],
        "destination_relative_path": row["planned_destination_relative_path"],
        "destination_absolute_path": str(target),
        "expected_size_bytes": row["size_bytes"],
        "actual_size_bytes": actual_size,
        "expected_sha256": row["sha256"],
        "actual_sha256": actual_hash,
        "copy_audit_record": copy_audit_record,
        "audit_status": status,
        "detail": detail,
    }


def _write_markdown_report(
    path: Path,
    *,
    rows: list[dict[str, str]],
    state_status: str,
    state_detail: str,
) -> None:
    status_counts = Counter(row["audit_status"] for row in rows)
    status_bytes = Counter()
    for row in rows:
        status_bytes[row["audit_status"]] += int(row["expected_size_bytes"])
    verified = status_counts["verified"]
    failures = len(rows) - verified
    copy_audit_missing = sum(row["copy_audit_record"] == "missing" for row in rows)
    lines = [
        "# Final Family Media Audit", "",
        f"Generated: `{_utc_now()}`", "",
        "## Safety", "",
        "- This audit reads only the approved plan, the local migration audit, and destination files.",
        "- It does not open, copy, rename, move, or delete any source or destination file.",
        "- Each destination file is re-hashed with SHA-256 and compared with the hash recorded in the approved plan.", "",
        "## Plan Pin", "",
        f"- Status: **{state_status}**",
        f"- Detail: {state_detail}", "",
        "## Summary", "",
        f"- Approved media rows audited: **{len(rows):,}**",
        f"- Destination files verified: **{verified:,}** / **{format_size(status_bytes['verified'])}**",
        f"- Audit issues: **{failures:,}**",
        f"- Missing successful migration-audit records: **{copy_audit_missing:,}**", "",
        "## Results", "", "| Status | Files | Expected Size |", "|---|---:|---:|",
    ]
    for status, count in status_counts.most_common():
        lines.append(f"| {status} | {count:,} | {format_size(status_bytes[status])} |")
    lines.extend([
        "", "## Interpretation", "",
        "- `verified` means the current destination file exists as a regular file, has the expected size, and its SHA-256 matches the approved plan.",
        "- Any other status requires review before treating Family Media as fully verified.",
        "- Original source files remain unchanged and are still the only copy until a second physical-drive backup exists.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_final_media_audit(
    project_name: str,
    *,
    destination: str,
    progress_every: int = 500,
) -> dict[str, int]:
    """Re-hash and validate every planned destination media file without changing data."""
    if progress_every <= 0:
        raise FinalMediaAuditError("--progress-every must be positive.")
    require_project(project_name)
    destination_root = resolved(destination)
    if destination_root.name != "Family Media" or not destination_root.exists() or not destination_root.is_dir() or destination_root.is_symlink():
        raise FinalMediaAuditError("Destination must be the existing non-symlink Family Media folder.")

    plan_rows = _planned_rows(project_name)
    successful_ids = _successful_copy_ids(project_name)
    state_status, state_detail = _plan_state_status(project_name)
    results: list[dict[str, str]] = []

    for index, row in enumerate(plan_rows, start=1):
        copy_audit_record = "present" if row["plan_id"] in successful_ids else "missing"
        try:
            target = _safe_target(destination_root, row["planned_destination_relative_path"])
        except FinalMediaAuditError as error:
            results.append(
                _report_row(
                    row,
                    target=destination_root,
                    actual_size="",
                    actual_hash="",
                    copy_audit_record=copy_audit_record,
                    status="unsafe-destination-path",
                    detail=str(error),
                )
            )
            continue

        expected_hash = row["sha256"].strip().casefold()
        if not expected_hash:
            results.append(
                _report_row(
                    row,
                    target=target,
                    actual_size="",
                    actual_hash="",
                    copy_audit_record=copy_audit_record,
                    status="plan-hash-missing",
                    detail="The approved plan has no SHA-256 for this row; destination cannot be fully verified.",
                )
            )
            continue
        if not target.exists():
            results.append(
                _report_row(
                    row,
                    target=target,
                    actual_size="",
                    actual_hash="",
                    copy_audit_record=copy_audit_record,
                    status="missing-destination-file",
                    detail="The approved destination file does not exist.",
                )
            )
            continue
        if target.is_symlink() or not target.is_file():
            results.append(
                _report_row(
                    row,
                    target=target,
                    actual_size="",
                    actual_hash="",
                    copy_audit_record=copy_audit_record,
                    status="destination-not-regular-file",
                    detail="Destination is a symlink or is not a regular file.",
                )
            )
            continue

        actual_size = target.stat().st_size
        if actual_size != int(row["size_bytes"]):
            results.append(
                _report_row(
                    row,
                    target=target,
                    actual_size=str(actual_size),
                    actual_hash="",
                    copy_audit_record=copy_audit_record,
                    status="destination-size-mismatch",
                    detail="Destination size does not match the approved plan.",
                )
            )
            continue

        actual_hash = _hash_file(target)
        if actual_hash.casefold() != expected_hash:
            results.append(
                _report_row(
                    row,
                    target=target,
                    actual_size=str(actual_size),
                    actual_hash=actual_hash,
                    copy_audit_record=copy_audit_record,
                    status="destination-hash-mismatch",
                    detail="Destination SHA-256 does not match the approved plan.",
                )
            )
            continue

        results.append(
            _report_row(
                row,
                target=target,
                actual_size=str(actual_size),
                actual_hash=actual_hash,
                copy_audit_record=copy_audit_record,
                status="verified",
                detail="Destination size and SHA-256 match the approved plan.",
            )
        )
        if index % progress_every == 0 or index == len(plan_rows):
            verified = sum(result["audit_status"] == "verified" for result in results)
            console.print(f"Audited {index:,}/{len(plan_rows):,} files; verified {verified:,}.")

    runtime = _runtime_dir(project_name)
    csv_path = runtime / "final_media_audit.csv"
    report_path = runtime / "final_media_audit.md"
    _write_csv_atomic(csv_path, results)
    _write_markdown_report(
        report_path,
        rows=results,
        state_status=state_status,
        state_detail=state_detail,
    )

    counts = Counter(result["audit_status"] for result in results)
    verified_bytes = sum(int(result["expected_size_bytes"]) for result in results if result["audit_status"] == "verified")
    table = Table(title="Final Family Media Audit — Report Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Approved media rows audited", f"{len(results):,}")
    table.add_row("Verified destination files", f"{counts['verified']:,}")
    table.add_row("Verified destination size", format_size(verified_bytes))
    table.add_row("Audit issues", f"{len(results) - counts['verified']:,}")
    table.add_row("Plan pin status", state_status)
    table.add_row("Missing copy-audit records", f"{sum(result['copy_audit_record'] == 'missing' for result in results):,}")
    console.print(table)
    console.print(f"[green]Detailed audit CSV:[/green] {csv_path}")
    console.print(f"[green]Audit report:[/green] {report_path}")
    console.print("[yellow]No source or destination file was changed.[/yellow]")

    return {
        "audited": len(results),
        "verified": counts["verified"],
        "issues": len(results) - counts["verified"],
    }
