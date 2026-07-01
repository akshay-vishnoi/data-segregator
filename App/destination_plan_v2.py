from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

from rich.table import Table

from .destination_plan import (
    PLAN_FIELDS,
    DestinationPlanError,
    PlanRecord,
    _assign_collision_safe_paths,
    _duplicate_selection,
    _hash_map,
    _metadata_map,
    _paths,
    _route,
    _row_for,
    _scope_map,
    _selected_sources,
    _write_json_atomic,
    _write_report,
    console,
)
from .project import source_runtime_dir
from .utils import format_size


def build_destination_dry_run_plan(
    project_name: str,
    *,
    source_labels: set[str] | None = None,
) -> tuple[Path, Path, dict[str, int]]:
    """Build the report-only family-media destination plan without performing any copy."""
    selected_sources = _selected_sources(project_name, source_labels)
    metadata = _metadata_map(project_name)
    scope = _scope_map(project_name)
    hashes = _hash_map(project_name)
    plan_path, summary_path, report_path = _paths(project_name)

    source_paths = {source["id"]: source["path"] for source in selected_sources}
    source_records: list[PlanRecord] = []
    included_media: list[PlanRecord] = []
    total_bytes = 0

    for source in selected_sources:
        manifest = source_runtime_dir(project_name, source["label"]) / "manifest.csv"
        if not manifest.exists():
            raise FileNotFoundError(f"No manifest found for source '{source['label']}'. Run a source scan first.")
        with manifest.open("r", newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                key = (row["source_id"], row["source_relative_path"])
                policy = scope.get(key)
                date = metadata.get(key)
                if policy is None:
                    raise DestinationPlanError(f"Scope policy has no row for: {row['source_relative_path']}")
                if date is None:
                    raise DestinationPlanError(f"Capture-date extraction has no row for: {row['source_relative_path']}")
                record = PlanRecord(
                    source_id=row["source_id"],
                    source_label=row["source_label"],
                    source_absolute_path=str(Path(source_paths[row["source_id"]]) / row["source_relative_path"]),
                    source_relative_path=row["source_relative_path"],
                    filename=row["filename"],
                    extension=row["extension"],
                    media_type=row["media_type"],
                    classification_confidence=row["confidence"],
                    size_bytes=int(row["size_bytes"]),
                    sha256=hashes.get(key, ""),
                    scope_status=policy["policy_status"],
                    scope_rule_id=policy["policy_rule_id"],
                    scope_reason=policy["policy_reason"],
                    date_source=date.get("date_source", "unknown") or "unknown",
                    capture_datetime=date.get("capture_datetime", ""),
                    capture_date=date.get("capture_date", ""),
                    date_precision=date.get("date_precision", ""),
                    metadata_tag=date.get("metadata_tag", ""),
                    metadata_value=date.get("metadata_value", ""),
                )
                source_records.append(record)
                total_bytes += record.size_bytes
                if record.scope_status == "family-media-candidate" and record.media_type in {"photo", "video"}:
                    included_media.append(record)

    duplicate_groups: dict[str, list[PlanRecord]] = defaultdict(list)
    for record in included_media:
        if record.sha256:
            duplicate_groups[record.sha256].append(record)

    duplicate_group_count = 0
    duplicate_confidence: Counter[str] = Counter()
    preferred_by_sha: dict[str, PlanRecord] = {}
    for sha256, records in duplicate_groups.items():
        if len(records) < 2:
            continue
        duplicate_group_count += 1
        selected, _, reasons, confidence = _duplicate_selection(records)
        duplicate_confidence[confidence] += 1
        preferred_by_sha[sha256] = selected
        for record in records:
            record.duplicate_copies_in_scope = len(records)
            record.duplicate_selection_confidence = confidence
            record.duplicate_selection_reasons = "; ".join(reasons)
            record.preferred_source_relative_path = selected.source_relative_path
            if record is selected:
                record.duplicate_role = "preferred-exact-duplicate"
            else:
                record.duplicate_role = "nonpreferred-exact-duplicate"
                record.planned_action = "skip-exact-duplicate"
                record.requires_review = confidence != "stronger-proposal"
                if record.requires_review:
                    record.review_reasons.append("duplicate-selection-" + confidence)
                record.routing_reason = "Exact bytes match the preferred source record; this source remains preserved with provenance only."

    copy_records: list[PlanRecord] = []
    for record in included_media:
        if record.duplicate_role == "nonpreferred-exact-duplicate":
            continue
        destination, route_review_reasons, routing_reason = _route(record)
        record.raw_destination_relative_path = destination
        record.routing_reason = routing_reason
        record.review_reasons.extend(route_review_reasons)
        if record.duplicate_role == "preferred-exact-duplicate" and record.duplicate_selection_confidence != "stronger-proposal":
            record.review_reasons.append("duplicate-selection-" + record.duplicate_selection_confidence)
        record.requires_review = bool(record.review_reasons)
        record.planned_action = "planned-copy-review" if record.requires_review else "planned-copy"
        copy_records.append(record)

    collision_renames = _assign_collision_safe_paths(copy_records)
    for record in included_media:
        if record.duplicate_role == "nonpreferred-exact-duplicate":
            preferred = preferred_by_sha[record.sha256]
            record.planned_destination_relative_path = preferred.planned_destination_relative_path
            record.destination_name_action = "uses-preferred-duplicate-destination"

    summary: dict[str, object] = {
        "generated_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "source_records": len(source_records),
        "source_bytes": total_bytes,
        "planned_copy_files": 0,
        "planned_copy_bytes": 0,
        "duplicate_skip_files": 0,
        "duplicate_skip_bytes": 0,
        "duplicate_groups": duplicate_group_count,
        "policy_excluded_files": 0,
        "policy_excluded_bytes": 0,
        "sidecar_review_files": 0,
        "sidecar_review_bytes": 0,
        "collision_renames": collision_renames,
        "routes": defaultdict(lambda: [0, 0]),
        "review_reasons": defaultdict(lambda: [0, 0]),
        "duplicate_confidence": dict(duplicate_confidence),
    }

    temporary = plan_path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PLAN_FIELDS)
        writer.writeheader()
        for record in source_records:
            if record.scope_status != "family-media-candidate":
                record.planned_action = "preserve-exclude-policy"
                record.routing_reason = "Excluded by local scope policy; source remains preserved in place."
                summary["policy_excluded_files"] += 1
                summary["policy_excluded_bytes"] += record.size_bytes
            elif record.media_type == "sidecar":
                record.planned_action = "preserve-review-sidecar"
                record.requires_review = True
                record.review_reasons.append("sidecar-companion-mapping-needed")
                record.routing_reason = "Sidecar is preserved pending a companion-file mapping stage."
                summary["sidecar_review_files"] += 1
                summary["sidecar_review_bytes"] += record.size_bytes
            elif record.duplicate_role == "nonpreferred-exact-duplicate":
                summary["duplicate_skip_files"] += 1
                summary["duplicate_skip_bytes"] += record.size_bytes
            else:
                summary["planned_copy_files"] += 1
                summary["planned_copy_bytes"] += record.size_bytes
                route = record.planned_destination_relative_path.split("/", 1)[0] if record.planned_destination_relative_path else "(none)"
                summary["routes"][route][0] += 1
                summary["routes"][route][1] += record.size_bytes

            for reason in record.review_reasons:
                summary["review_reasons"][reason][0] += 1
                summary["review_reasons"][reason][1] += record.size_bytes
            writer.writerow(_row_for(record))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(plan_path)

    serializable = {
        **{key: value for key, value in summary.items() if key not in {"routes", "review_reasons"}},
        "routes": dict(summary["routes"]),
        "review_reasons": dict(summary["review_reasons"]),
    }
    _write_json_atomic(summary_path, serializable)
    _write_report(report_path, serializable)

    table = Table(title="Destination Dry-Run Plan — No Files Copied")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Future-copy candidates", f"{summary['planned_copy_files']:,}")
    table.add_row("Planned copy size", format_size(summary["planned_copy_bytes"]))
    table.add_row("Exact duplicate records skipped", f"{summary['duplicate_skip_files']:,}")
    table.add_row("Policy-preserved exclusions", f"{summary['policy_excluded_files']:,}")
    table.add_row("Sidecars held for review", f"{summary['sidecar_review_files']:,}")
    table.add_row("Collision-safe name changes", f"{collision_renames:,}")
    console.print(table)
    console.print(f"[green]Dry-run plan:[/green] {plan_path}")
    console.print(f"[green]Plan report:[/green] {report_path}")
    console.print("[yellow]No source or destination files were copied, moved, renamed, or deleted.[/yellow]")
    return plan_path, report_path, {key: int(value) for key, value in serializable.items() if isinstance(value, int)}
