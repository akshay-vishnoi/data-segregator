from __future__ import annotations

import csv
import hashlib
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .canonical import DuplicateCandidate, _proposal_confidence, _score_candidate
from .db import database_path, get_connection
from .project import project_dir, require_project, source_runtime_dir
from .utils import format_size

console = Console()

TRUSTED_DATE_SOURCES = frozenset({"photo-exif", "video-embedded", "filename"})
DATE_VALUE = re.compile(r"^(19\d{2}|20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
INVALID_DESTINATION_SEGMENT = re.compile(r"[\\/:*?\"<>|\x00-\x1f]")

PLAN_FIELDS = [
    "plan_id",
    "planned_action",
    "requires_review",
    "review_reasons",
    "source_id",
    "source_label",
    "source_absolute_path",
    "source_relative_path",
    "filename",
    "extension",
    "media_type",
    "classification_confidence",
    "size_bytes",
    "sha256",
    "scope_status",
    "scope_rule_id",
    "scope_reason",
    "date_source",
    "capture_datetime",
    "capture_date",
    "date_precision",
    "metadata_tag",
    "metadata_value",
    "duplicate_copies_in_scope",
    "duplicate_role",
    "duplicate_selection_confidence",
    "duplicate_selection_reasons",
    "preferred_source_relative_path",
    "planned_destination_relative_path",
    "destination_name_action",
    "routing_reason",
]


class DestinationPlanError(RuntimeError):
    pass


@dataclass
class PlanRecord:
    source_id: str
    source_label: str
    source_absolute_path: str
    source_relative_path: str
    filename: str
    extension: str
    media_type: str
    classification_confidence: str
    size_bytes: int
    sha256: str
    scope_status: str
    scope_rule_id: str
    scope_reason: str
    date_source: str
    capture_datetime: str
    capture_date: str
    date_precision: str
    metadata_tag: str
    metadata_value: str
    duplicate_copies_in_scope: int = 1
    duplicate_role: str = "unique-or-unhashed"
    duplicate_selection_confidence: str = "not-applicable"
    duplicate_selection_reasons: str = ""
    preferred_source_relative_path: str = ""
    planned_action: str = ""
    requires_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    raw_destination_relative_path: str = ""
    planned_destination_relative_path: str = ""
    destination_name_action: str = "preserve-original-name"
    routing_reason: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return self.source_id, self.source_relative_path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(project_name: str) -> tuple[Path, Path, Path]:
    require_project(project_name)
    plans_dir = project_dir(project_name) / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    return (
        plans_dir / "destination_dry_run_plan.csv",
        plans_dir / "destination_dry_run_plan_summary.json",
        project_dir(project_name) / "reports" / "destination_dry_run_plan.md",
    )


def _read_csv_map(path: Path, *, required_fields: set[str], label: str) -> dict[tuple[str, str], dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing {label}: {path}")
    records: dict[tuple[str, str], dict[str, str]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = set(reader.fieldnames or [])
        missing = required_fields - fieldnames
        if missing:
            raise DestinationPlanError(f"{label} is missing columns: {', '.join(sorted(missing))}")
        for row in reader:
            source_id = row.get("source_id", "")
            relative_path = row.get("source_relative_path", "")
            if source_id and relative_path:
                records[(source_id, relative_path)] = row
    return records


def _metadata_map(project_name: str) -> dict[tuple[str, str], dict[str, str]]:
    path = project_dir(project_name) / "metadata-v2" / "capture_dates.csv"
    return _read_csv_map(
        path,
        required_fields={"source_id", "source_relative_path", "date_source", "capture_date"},
        label="completed V2 capture-date CSV",
    )


def _scope_map(project_name: str) -> dict[tuple[str, str], dict[str, str]]:
    path = project_dir(project_name) / "policy" / "scope_policy.csv"
    return _read_csv_map(
        path,
        required_fields={"source_id", "source_relative_path", "policy_status", "policy_rule_id", "policy_reason"},
        label="scope-policy CSV",
    )


def _hash_map(project_name: str) -> dict[tuple[str, str], str]:
    path = database_path(project_name)
    if not path.exists():
        raise FileNotFoundError("Media catalog database is missing. Run: data-segregator index <project>")
    conn = get_connection(project_name)
    try:
        return {
            (row["source_id"], row["source_relative_path"]): row["sha256"] or ""
            for row in conn.execute("SELECT source_id, source_relative_path, sha256 FROM media_files")
        }
    finally:
        conn.close()


def _selected_sources(project_name: str, source_labels: set[str] | None) -> list[dict]:
    project = require_project(project_name)
    known_labels = {source["label"] for source in project["sources"]}
    unknown_labels = (source_labels or set()) - known_labels
    if unknown_labels:
        raise ValueError(f"Unknown source label(s): {', '.join(sorted(unknown_labels))}")
    return [source for source in project["sources"] if not source_labels or source["label"] in source_labels]


def _safe_segment(value: str) -> str:
    cleaned = INVALID_DESTINATION_SEGMENT.sub("_", value).strip().rstrip(".")
    return cleaned or "_"


def _safe_relative_path(relative_path: str) -> str:
    return "/".join(_safe_segment(part) for part in relative_path.replace("\\", "/").split("/") if part not in {"", ".", ".."})


def _valid_date(value: str) -> bool:
    if not DATE_VALUE.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _bucket(media_type: str) -> str:
    if media_type == "photo":
        return "Photos"
    if media_type == "video":
        return "Videos"
    raise DestinationPlanError(f"Unsupported media type for routing: {media_type}")


def _route(record: PlanRecord) -> tuple[str, list[str], str]:
    """Return raw destination, review reasons, and routing explanation for a candidate media record."""
    bucket = _bucket(record.media_type)
    safe_source_path = _safe_relative_path(record.source_relative_path)

    if record.classification_confidence != "definite":
        return (
            f"Needs Date Review/{bucket}/{safe_source_path}",
            ["candidate-media-type"],
            "Candidate media type stays in review with original folder context.",
        )

    if record.date_source in TRUSTED_DATE_SOURCES and _valid_date(record.capture_date):
        year, month, _ = record.capture_date.split("-")
        filename = _safe_segment(record.filename)
        return (
            f"{bucket}/{year}/{year}-{month}/{filename}",
            [],
            f"Trusted {record.date_source} date routes to the main date library.",
        )

    if record.date_source == "unknown" or not record.capture_date:
        return (
            f"Unknown Date/{bucket}/{safe_source_path}",
            ["unknown-date"],
            "No usable embedded, filename, or filesystem date was recorded.",
        )

    return (
        f"Needs Date Review/{bucket}/{safe_source_path}",
        ["filesystem-date-only"],
        "Filesystem-modified time is retained as evidence but not treated as a trusted capture date.",
    )


def _duplicate_selection(records: list[PlanRecord]) -> tuple[PlanRecord, int, tuple[str, ...], str]:
    scored: list[tuple[PlanRecord, int, tuple[str, ...]]] = []
    for record in records:
        candidate = DuplicateCandidate(
            sha256=record.sha256,
            copies=len(records),
            size_bytes=record.size_bytes,
            source_label=record.source_label,
            source_relative_path=record.source_relative_path,
            filename=record.filename,
            media_type=record.media_type,
            confidence=record.classification_confidence,
        )
        score, reasons = _score_candidate(candidate)
        scored.append((record, score, reasons))
    scored.sort(key=lambda item: (-item[1], item[0].source_relative_path.casefold(), item[0].source_label.casefold()))
    selected, best_score, reasons = scored[0]
    second_score = scored[1][1] if len(scored) > 1 else best_score
    return selected, best_score, reasons, _proposal_confidence(best_score, best_score - second_score)


def _stable_collision_token(record: PlanRecord) -> str:
    if record.sha256:
        return record.sha256[:12]
    identity = f"{record.source_id}\0{record.source_relative_path}".encode("utf-8")
    return "src-" + hashlib.sha256(identity).hexdigest()[:12]


def _with_collision_suffix(raw_path: str, record: PlanRecord) -> str:
    path = Path(raw_path)
    suffix = path.suffix
    stem = path.name[:-len(suffix)] if suffix else path.name
    replacement = f"{stem}__{_stable_collision_token(record)}{suffix}"
    return str(path.with_name(replacement)).replace("\\", "/")


def _assign_collision_safe_paths(copy_records: list[PlanRecord]) -> int:
    by_raw_path: dict[str, list[PlanRecord]] = defaultdict(list)
    for record in copy_records:
        by_raw_path[record.raw_destination_relative_path.casefold()].append(record)

    renamed = 0
    used: set[str] = set()
    for _, records in by_raw_path.items():
        if len(records) == 1:
            record = records[0]
            destination = record.raw_destination_relative_path
            if destination.casefold() in used:
                destination = _with_collision_suffix(destination, record)
                record.destination_name_action = "source-identity-suffix"
                renamed += 1
            record.planned_destination_relative_path = destination
            used.add(destination.casefold())
            continue

        for record in sorted(records, key=lambda item: (item.source_relative_path.casefold(), item.source_id)):
            destination = _with_collision_suffix(record.raw_destination_relative_path, record)
            counter = 2
            candidate = destination
            while candidate.casefold() in used:
                path = Path(destination)
                suffix = path.suffix
                stem = path.name[:-len(suffix)] if suffix else path.name
                candidate = str(path.with_name(f"{stem}--{counter}{suffix}")).replace("\\", "/")
                counter += 1
            record.planned_destination_relative_path = candidate
            record.destination_name_action = "content-or-source-suffix-for-conflict"
            used.add(candidate.casefold())
            renamed += 1
    return renamed


def _write_json_atomic(path: Path, payload: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    with temporary.open("r+", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _row_for(record: PlanRecord) -> dict[str, str | int]:
    return {
        "plan_id": hashlib.sha256(f"{record.source_id}\0{record.source_relative_path}".encode("utf-8")).hexdigest()[:16],
        "planned_action": record.planned_action,
        "requires_review": "yes" if record.requires_review else "no",
        "review_reasons": "; ".join(record.review_reasons),
        "source_id": record.source_id,
        "source_label": record.source_label,
        "source_absolute_path": record.source_absolute_path,
        "source_relative_path": record.source_relative_path,
        "filename": record.filename,
        "extension": record.extension,
        "media_type": record.media_type,
        "classification_confidence": record.classification_confidence,
        "size_bytes": record.size_bytes,
        "sha256": record.sha256,
        "scope_status": record.scope_status,
        "scope_rule_id": record.scope_rule_id,
        "scope_reason": record.scope_reason,
        "date_source": record.date_source,
        "capture_datetime": record.capture_datetime,
        "capture_date": record.capture_date,
        "date_precision": record.date_precision,
        "metadata_tag": record.metadata_tag,
        "metadata_value": record.metadata_value,
        "duplicate_copies_in_scope": record.duplicate_copies_in_scope,
        "duplicate_role": record.duplicate_role,
        "duplicate_selection_confidence": record.duplicate_selection_confidence,
        "duplicate_selection_reasons": record.duplicate_selection_reasons,
        "preferred_source_relative_path": record.preferred_source_relative_path,
        "planned_destination_relative_path": record.planned_destination_relative_path,
        "destination_name_action": record.destination_name_action,
        "routing_reason": record.routing_reason,
    }


def _write_report(report_path: Path, summary: dict) -> None:
    lines = [
        "# Destination Dry-Run Plan", "",
        f"Generated: `{_utc_now()}`", "",
        "## Safety", "",
        "- This is a planning report only. No source file was copied, moved, renamed, or deleted.",
        "- `projects/**` remains preserved but excluded by the configured scope policy.",
        "- Exact duplicate source paths are retained in the plan as provenance; only one deterministic representative is marked for a future copy.",
        "- Files with filesystem-only dates or candidate media types remain in a review route rather than the main date library.", "",
        "## Plan Summary", "",
        f"- Source records evaluated: **{summary['source_records']:,}**",
        f"- Source size represented: **{format_size(summary['source_bytes'])}**",
        f"- Future-copy candidates: **{summary['planned_copy_files']:,}** files / **{format_size(summary['planned_copy_bytes'])}**",
        f"- Exact duplicate source records skipped in a future copy: **{summary['duplicate_skip_files']:,}** / **{format_size(summary['duplicate_skip_bytes'])}**",
        f"- Exact duplicate groups in family-media scope: **{summary['duplicate_groups']:,}**",
        f"- Policy-preserved exclusions: **{summary['policy_excluded_files']:,}** / **{format_size(summary['policy_excluded_bytes'])}**",
        f"- Sidecars held for later companion-file review: **{summary['sidecar_review_files']:,}** / **{format_size(summary['sidecar_review_bytes'])}**",
        f"- Destination filename changes needed for collisions: **{summary['collision_renames']:,}**", "",
        "## Future Copy Routes", "", "| Route | Files | Size |", "|---|---:|---:|",
    ]
    for route, values in sorted(summary["routes"].items(), key=lambda item: item[1][0], reverse=True):
        lines.append(f"| {route} | {values[0]:,} | {format_size(values[1])} |")

    lines.extend(["", "## Review Reasons", "", "| Reason | Files | Size |", "|---|---:|---:|"])
    for reason, values in sorted(summary["review_reasons"].items(), key=lambda item: item[1][0], reverse=True):
        lines.append(f"| {reason} | {values[0]:,} | {format_size(values[1])} |")

    lines.extend(["", "## Duplicate Selection Confidence", "", "| Confidence | Groups |", "|---|---:|"])
    for confidence, count in sorted(summary["duplicate_confidence"].items(), key=lambda item: item[1], reverse=True):
        lines.append(f"| {confidence} | {count:,} |")

    lines.extend([
        "", "## Planned Destination Rules", "",
        "- Trusted embedded or clearly filename-derived dates: `Photos/YYYY/YYYY-MM/` or `Videos/YYYY/YYYY-MM/`.",
        "- Filesystem-only dates and candidate media types: `Needs Date Review/<Photos|Videos>/<original source path>`.",
        "- No usable date: `Unknown Date/<Photos|Videos>/<original source path>`.",
        "- Sidecars are not automatically routed because their link to a moved media file needs a dedicated companion-file stage.",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_destination_dry_run_plan(
    project_name: str,
    *,
    source_labels: set[str] | None = None,
) -> tuple[Path, Path, dict[str, int]]:
    """Create a complete report-only, collision-safe destination plan; no file operation occurs."""
    selected_sources = _selected_sources(project_name, source_labels)
    metadata = _metadata_map(project_name)
    scope = _scope_map(project_name)
    hashes = _hash_map(project_name)
    plan_path, summary_path, report_path = _paths(project_name)

    selected_source_ids = {source["id"] for source in selected_sources}
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
    preferred_by_key: dict[tuple[str, str], PlanRecord] = {}
    for records in duplicate_groups.values():
        if len(records) == 1:
            continue
        duplicate_group_count += 1
        selected, _, reasons, confidence = _duplicate_selection(records)
        duplicate_confidence[confidence] += 1
        for record in records:
            record.duplicate_copies_in_scope = len(records)
            record.duplicate_selection_confidence = confidence
            record.duplicate_selection_reasons = "; ".join(reasons)
            record.preferred_source_relative_path = selected.source_relative_path
            if record is selected:
                record.duplicate_role = "preferred-exact-duplicate"
                preferred_by_key[record.key] = record
            else:
                record.duplicate_role = "nonpreferred-exact-duplicate"
                record.planned_action = "skip-exact-duplicate"
                record.requires_review = confidence != "stronger-proposal"
                if confidence != "stronger-proposal":
                    record.review_reasons.append("duplicate-selection-" + confidence)
                record.routing_reason = "Exact bytes match a preferred source record; this source remains preserved with provenance only."

    copy_records: list[PlanRecord] = []
    for record in included_media:
        if record.duplicate_role == "nonpreferred-exact-duplicate":
            continue
        raw_destination, route_reviews, routing_reason = _route(record)
        record.raw_destination_relative_path = raw_destination
        record.routing_reason = routing_reason
        record.review_reasons.extend(route_reviews)
        if record.duplicate_role == "preferred-exact-duplicate" and record.duplicate_selection_confidence != "stronger-proposal":
            record.review_reasons.append("duplicate-selection-" + record.duplicate_selection_confidence)
        record.requires_review = bool(record.review_reasons)
        record.planned_action = "planned-copy-review" if record.requires_review else "planned-copy"
        copy_records.append(record)

    collision_renames = _assign_collision_safe_paths(copy_records)
    preferred_destinations = {record.key: record.planned_destination_relative_path for record in copy_records}
    for record in included_media:
        if record.duplicate_role == "nonpreferred-exact-duplicate":
            preferred_key = next(
                (key for key, preferred in preferred_by_key.items() if preferred.source_relative_path == record.preferred_source_relative_path and preferred.sha256 == record.sha256),
                None,
            )
            if preferred_key:
                record.planned_destination_relative_path = preferred_destinations.get(preferred_key, "")
                record.destination_name_action = "uses-preferred-duplicate-destination"

    summary: dict[str, object] = {
        "generated_at": _utc_now(),
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

    serializable_summary = {
        **{key: value for key, value in summary.items() if key not in {"routes", "review_reasons"}},
        "routes": dict(summary["routes"]),
        "review_reasons": dict(summary["review_reasons"]),
    }
    _write_json_atomic(summary_path, serializable_summary)
    _write_report(report_path, serializable_summary)

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

    return plan_path, report_path, {key: int(value) for key, value in serializable_summary.items() if isinstance(value, int)}
