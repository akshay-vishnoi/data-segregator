from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.table import Table

from .destination_plan import PLAN_FIELDS, DestinationPlanError, _paths, _write_report, console
from .utils import format_size


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with temporary.open("r+", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def finalize_destination_dry_run_plan(project_name: str) -> tuple[Path, Path, dict[str, int]]:
    """Build the missing JSON/Markdown summary from an already completed plan CSV only."""
    plan_path, summary_path, report_path = _paths(project_name)
    if not plan_path.exists():
        raise FileNotFoundError(f"Dry-run plan CSV does not exist: {plan_path}")

    source_records = 0
    source_bytes = 0
    planned_copy_files = 0
    planned_copy_bytes = 0
    duplicate_skip_files = 0
    duplicate_skip_bytes = 0
    policy_excluded_files = 0
    policy_excluded_bytes = 0
    sidecar_review_files = 0
    sidecar_review_bytes = 0
    collision_renames = 0
    routes: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    review_reasons: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    duplicate_confidence: Counter[str] = Counter()

    with plan_path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = set(reader.fieldnames or [])
        missing = set(PLAN_FIELDS) - fields
        if missing:
            raise DestinationPlanError(f"Existing plan CSV is incomplete; missing columns: {', '.join(sorted(missing))}")

        for row in reader:
            source_records += 1
            size = int(row["size_bytes"])
            source_bytes += size
            action = row["planned_action"]

            if action in {"planned-copy", "planned-copy-review"}:
                planned_copy_files += 1
                planned_copy_bytes += size
                destination = row["planned_destination_relative_path"]
                route = destination.split("/", 1)[0] if destination else "(none)"
                routes[route][0] += 1
                routes[route][1] += size
            elif action == "skip-exact-duplicate":
                duplicate_skip_files += 1
                duplicate_skip_bytes += size
            elif action == "preserve-exclude-policy":
                policy_excluded_files += 1
                policy_excluded_bytes += size
            elif action == "preserve-review-sidecar":
                sidecar_review_files += 1
                sidecar_review_bytes += size

            if row["destination_name_action"] not in {
                "",
                "preserve-original-name",
                "uses-preferred-duplicate-destination",
            }:
                collision_renames += 1

            for reason in (item.strip() for item in row["review_reasons"].split(";") if item.strip()):
                review_reasons[reason][0] += 1
                review_reasons[reason][1] += size

            if row["duplicate_role"] == "preferred-exact-duplicate":
                confidence = row["duplicate_selection_confidence"] or "unknown"
                duplicate_confidence[confidence] += 1

    summary: dict[str, object] = {
        "generated_at": _utc_now(),
        "source_records": source_records,
        "source_bytes": source_bytes,
        "planned_copy_files": planned_copy_files,
        "planned_copy_bytes": planned_copy_bytes,
        "duplicate_skip_files": duplicate_skip_files,
        "duplicate_skip_bytes": duplicate_skip_bytes,
        "duplicate_groups": sum(duplicate_confidence.values()),
        "policy_excluded_files": policy_excluded_files,
        "policy_excluded_bytes": policy_excluded_bytes,
        "sidecar_review_files": sidecar_review_files,
        "sidecar_review_bytes": sidecar_review_bytes,
        "collision_renames": collision_renames,
        "routes": dict(routes),
        "review_reasons": dict(review_reasons),
        "duplicate_confidence": dict(duplicate_confidence),
    }
    _write_json_atomic(summary_path, summary)
    _write_report(report_path, summary)

    table = Table(title="Destination Dry-Run Plan — Summary Finalized")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Future-copy candidates", f"{planned_copy_files:,}")
    table.add_row("Planned copy size", format_size(planned_copy_bytes))
    table.add_row("Exact duplicate records skipped", f"{duplicate_skip_files:,}")
    table.add_row("Policy-preserved exclusions", f"{policy_excluded_files:,}")
    table.add_row("Sidecars held for review", f"{sidecar_review_files:,}")
    table.add_row("Collision-safe name changes", f"{collision_renames:,}")
    console.print(table)
    console.print(f"[green]Dry-run plan:[/green] {plan_path}")
    console.print(f"[green]Plan report:[/green] {report_path}")
    console.print("[yellow]Finalized from the existing local plan CSV. No source or destination files were changed.[/yellow]")
    return plan_path, report_path, {key: int(value) for key, value in summary.items() if isinstance(value, int)}
