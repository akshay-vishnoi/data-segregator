from __future__ import annotations

import csv
import hashlib
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
SUCCESS_STATUSES = {"copied-and-verified", "already-verified"}
PLAN_FIELDS = [
    "plan_id",
    "destination_root",
    "destination_relative_path",
    "destination_absolute_path",
    "size_bytes",
    "expected_sha256",
    "current_destination_sha256",
    "new_plan_action",
    "new_scope_rule_id",
    "cleanup_status",
    "reason",
]


class PilotCleanupPlanError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_csv_atomic(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _audit_rows(project_name: str) -> dict[str, dict[str, str]]:
    audit_path = project_dir(project_name) / "pilot_copy" / "pilot_copy_audit.csv"
    if not audit_path.exists():
        raise PilotCleanupPlanError(f"Pilot audit is missing: {audit_path}")
    latest: dict[str, dict[str, str]] = {}
    with audit_path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") in SUCCESS_STATUSES and row.get("plan_id"):
                latest[row["plan_id"]] = row
    if not latest:
        raise PilotCleanupPlanError("Pilot audit has no verified copied files.")
    return latest


def build_pilot_cleanup_plan(project_name: str, *, destination: str) -> dict[str, int]:
    """Create a report-only plan for pilot files that are now excluded by policy."""
    require_project(project_name)
    destination_root = resolved(destination)
    if destination_root.name != "Family Media" or not destination_root.is_dir():
        raise PilotCleanupPlanError("Destination must be the existing Family Media folder.")

    plan_by_id = {row["plan_id"]: row for row in _read_plan(project_name)}
    audit_by_id = _audit_rows(project_name)
    rows: list[dict[str, str]] = []

    for plan_id, audit in sorted(audit_by_id.items()):
        current = plan_by_id.get(plan_id)
        relative = audit["planned_destination_relative_path"]
        target = destination_root / relative
        expected_hash = audit["expected_sha256"]
        size = audit["size_bytes"]
        base = {
            "plan_id": plan_id,
            "destination_root": str(destination_root),
            "destination_relative_path": relative,
            "destination_absolute_path": str(target),
            "size_bytes": size,
            "expected_sha256": expected_hash,
            "current_destination_sha256": "",
            "new_plan_action": current["planned_action"] if current else "missing-from-current-plan",
            "new_scope_rule_id": current["scope_rule_id"] if current else "",
            "cleanup_status": "keep",
            "reason": "Still included by the current plan.",
        }
        if current is None:
            base["cleanup_status"] = "hold-for-review"
            base["reason"] = "The earlier pilot record is absent from the current plan."
        elif current["planned_action"] != "preserve-exclude-policy":
            base["reason"] = "Still included or otherwise preserved by the current plan."
        elif not target.exists() or not target.is_file():
            base["cleanup_status"] = "hold-for-review"
            base["reason"] = "Current policy excludes this file, but the pilot destination file is missing."
        elif target.stat().st_size != int(size):
            base["cleanup_status"] = "hold-for-review"
            base["reason"] = "Current policy excludes this file, but the destination size no longer matches the verified pilot record."
        else:
            destination_hash = _hash_file(target)
            base["current_destination_sha256"] = destination_hash
            if destination_hash == expected_hash:
                base["cleanup_status"] = "safe-cleanup-candidate"
                base["reason"] = "Current policy excludes this verified pilot file; a future explicit cleanup may remove only this destination copy."
            else:
                base["cleanup_status"] = "hold-for-review"
                base["reason"] = "Current policy excludes this file, but its destination hash no longer matches the verified pilot record."
        rows.append(base)

    output_dir = project_dir(project_name) / "pilot_copy"
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "pilot_cleanup_plan.csv"
    report_path = output_dir / "pilot_cleanup_plan.md"
    _write_csv_atomic(csv_path, rows)

    counts = Counter(row["cleanup_status"] for row in rows)
    safe_rows = [row for row in rows if row["cleanup_status"] == "safe-cleanup-candidate"]
    safe_bytes = sum(int(row["size_bytes"]) for row in safe_rows)
    report_lines = [
        "# Pilot Cleanup Plan", "",
        f"Generated: `{_utc_now()}`", "",
        "## Safety", "",
        "- This command does not remove, move, rename, or change any file.",
        "- A safe cleanup candidate is a pilot destination file that the current scope policy now excludes and whose current SHA-256 still matches the verified pilot record.",
        "- Original source files are never cleanup candidates.", "",
        "## Summary", "",
        f"- Verified pilot files evaluated: **{len(rows):,}**",
        f"- Safe cleanup candidates: **{len(safe_rows):,}** / **{format_size(safe_bytes)}**",
        f"- Kept or held for review: **{len(rows) - len(safe_rows):,}**", "",
        "## Status", "", "| Status | Files |", "|---|---:|",
    ]
    for status, count in counts.most_common():
        report_lines.append(f"| {status} | {count:,} |")
    report_lines.extend([
        "", "## Next", "",
        "Review `pilot_cleanup_plan.csv`. No cleanup has occurred. A separate explicit command is required before any destination pilot file can be removed.",
    ])
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    table = Table(title="Pilot Cleanup Plan — No Files Changed")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Verified pilot files evaluated", f"{len(rows):,}")
    table.add_row("Safe cleanup candidates", f"{len(safe_rows):,}")
    table.add_row("Safe cleanup candidate size", format_size(safe_bytes))
    table.add_row("Keep or hold for review", f"{len(rows) - len(safe_rows):,}")
    console.print(table)
    console.print(f"[green]Cleanup plan:[/green] {csv_path}")
    console.print("[yellow]No file was changed.[/yellow]")
    return {"evaluated": len(rows), "safe_cleanup_candidates": len(safe_rows)}
