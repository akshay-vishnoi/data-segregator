from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project, source_runtime_dir
from .utils import format_size

console = Console()

POLICY_FIELDS = [
    "source_id",
    "source_label",
    "source_relative_path",
    "media_type",
    "extension",
    "size_bytes",
    "policy_status",
    "export_scope",
    "policy_rule_id",
    "policy_reason",
]

DEFAULT_POLICY = {
    "schema_version": 1,
    "description": "Local report-only scope policy. It never modifies source files.",
    "rules": [
        {
            "id": "preserve-exclude-projects",
            "path_prefix": "projects",
            "policy_status": "preserve-exclude",
            "export_scope": "exclude-default",
            "reason": (
                "Software project archive: preserve in place, but exclude from the "
                "first family-media export unless explicitly included later."
            ),
        }
    ],
    "default": {
        "policy_status": "family-media-candidate",
        "export_scope": "include-review",
        "reason": "Included in the family-media review scope; no export or file change is performed.",
    },
}


class ScopePolicyError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _paths(project_name: str) -> tuple[Path, Path, Path]:
    require_project(project_name)
    policy_dir = project_dir(project_name) / "policy"
    policy_dir.mkdir(parents=True, exist_ok=True)
    return (
        policy_dir / "scope_policy.json",
        policy_dir / "scope_policy.csv",
        project_dir(project_name) / "reports" / "scope_policy_report.md",
    )


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    with temporary.open("r+", encoding="utf-8") as handle:
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _load_or_create_policy(path: Path) -> dict:
    if not path.exists():
        _write_json_atomic(path, DEFAULT_POLICY)
        return DEFAULT_POLICY

    try:
        policy = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ScopePolicyError(f"Invalid local scope policy JSON: {path}") from error

    if not isinstance(policy, dict) or not isinstance(policy.get("rules"), list):
        raise ScopePolicyError("Scope policy must contain a 'rules' list.")
    if not isinstance(policy.get("default"), dict):
        raise ScopePolicyError("Scope policy must contain a 'default' object.")

    for rule in policy["rules"]:
        required = {"id", "path_prefix", "policy_status", "export_scope", "reason"}
        if not isinstance(rule, dict) or not required.issubset(rule):
            raise ScopePolicyError("Every scope policy rule requires id, path_prefix, policy_status, export_scope, and reason.")
    required_default = {"policy_status", "export_scope", "reason"}
    if not required_default.issubset(policy["default"]):
        raise ScopePolicyError("Scope policy default requires policy_status, export_scope, and reason.")
    return policy


def _normalised_path(value: str) -> str:
    return value.replace("\\", "/").strip("/")


def _matches_prefix(relative_path: str, prefix: str) -> bool:
    candidate = _normalised_path(relative_path)
    normalised_prefix = _normalised_path(prefix)
    return candidate == normalised_prefix or candidate.startswith(normalised_prefix + "/")


def _decision(policy: dict, relative_path: str) -> dict:
    for rule in policy["rules"]:
        if _matches_prefix(relative_path, str(rule["path_prefix"])):
            return {
                "policy_status": str(rule["policy_status"]),
                "export_scope": str(rule["export_scope"]),
                "policy_rule_id": str(rule["id"]),
                "policy_reason": str(rule["reason"]),
            }

    default = policy["default"]
    return {
        "policy_status": str(default["policy_status"]),
        "export_scope": str(default["export_scope"]),
        "policy_rule_id": "default-family-media-review",
        "policy_reason": str(default["reason"]),
    }


def _source_manifest(project_name: str, source_label: str) -> Path:
    manifest = source_runtime_dir(project_name, source_label) / "manifest.csv"
    if not manifest.exists():
        raise FileNotFoundError(f"No manifest found for source '{source_label}'. Run a source scan first.")
    return manifest


def _write_report(
    report_path: Path,
    *,
    total_files: int,
    total_bytes: int,
    by_status: Counter[str],
    bytes_by_status: Counter[str],
    by_rule: Counter[str],
    bytes_by_rule: Counter[str],
    excluded_top_folders: dict[str, list[int]],
) -> None:
    lines = [
        "# Family Media Scope Policy", "",
        f"Generated: `{_utc_now()}`", "",
        "## Policy", "",
        "- `projects/**` is classified as **preserve-exclude**.",
        "- Preserve-exclude means the source stays untouched and is excluded from a future family-media export by default.",
        "- All other source paths remain **family-media-candidate** for review; this is not an export decision.",
        "- This command only creates local policy/report files. It does not copy, rename, move, or delete source files.", "",
        "## Summary by Status", "", "| Status | Files | Size |", "|---|---:|---:|",
    ]
    for status, count in by_status.most_common():
        lines.append(f"| {status} | {count:,} | {format_size(bytes_by_status[status])} |")

    lines.extend(["", "## Summary by Rule", "", "| Rule | Files | Size |", "|---|---:|---:|"])
    for rule, count in by_rule.most_common():
        lines.append(f"| {rule} | {count:,} | {format_size(bytes_by_rule[rule])} |")

    lines.extend(["", "## Preserved but Excluded Top-Level Folders", "", "| Folder | Files | Size |", "|---|---:|---:|"])
    for folder, values in sorted(excluded_top_folders.items(), key=lambda item: item[1][0], reverse=True):
        lines.append(f"| {folder} | {values[0]:,} | {format_size(values[1])} |")

    lines.extend([
        "", "## Totals", "",
        f"- Manifest records evaluated: **{total_files:,}**",
        f"- Total size represented: **{format_size(total_bytes)}**",
    ])
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_scope_policy(project_name: str, *, source_labels: set[str] | None = None) -> tuple[Path, Path, dict[str, int]]:
    """Create a local, report-only scope map from completed source manifests."""
    project = require_project(project_name)
    all_labels = {source["label"] for source in project["sources"]}
    unknown_labels = (source_labels or set()) - all_labels
    if unknown_labels:
        raise ValueError(f"Unknown source label(s): {', '.join(sorted(unknown_labels))}")

    selected_sources = [source for source in project["sources"] if not source_labels or source["label"] in source_labels]
    policy_path, scope_csv_path, report_path = _paths(project_name)
    policy = _load_or_create_policy(policy_path)

    by_status: Counter[str] = Counter()
    bytes_by_status: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    bytes_by_rule: Counter[str] = Counter()
    excluded_top_folders: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    total_files = 0
    total_bytes = 0

    temporary = scope_csv_path.with_suffix(".csv.tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=POLICY_FIELDS)
        writer.writeheader()
        for source in selected_sources:
            manifest = _source_manifest(project_name, source["label"])
            with manifest.open("r", newline="", encoding="utf-8") as manifest_handle:
                for row in csv.DictReader(manifest_handle):
                    decision = _decision(policy, row["source_relative_path"])
                    size = int(row["size_bytes"])
                    total_files += 1
                    total_bytes += size
                    by_status[decision["policy_status"]] += 1
                    bytes_by_status[decision["policy_status"]] += size
                    by_rule[decision["policy_rule_id"]] += 1
                    bytes_by_rule[decision["policy_rule_id"]] += size
                    if decision["policy_status"] == "preserve-exclude":
                        top_folder = _normalised_path(row["source_relative_path"]).split("/", 1)[0] or "(root)"
                        excluded_top_folders[top_folder][0] += 1
                        excluded_top_folders[top_folder][1] += size
                    writer.writerow({
                        "source_id": row["source_id"],
                        "source_label": row["source_label"],
                        "source_relative_path": row["source_relative_path"],
                        "media_type": row["media_type"],
                        "extension": row["extension"],
                        "size_bytes": row["size_bytes"],
                        **decision,
                    })
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(scope_csv_path)

    _write_report(
        report_path,
        total_files=total_files,
        total_bytes=total_bytes,
        by_status=by_status,
        bytes_by_status=bytes_by_status,
        by_rule=by_rule,
        bytes_by_rule=bytes_by_rule,
        excluded_top_folders=excluded_top_folders,
    )

    table = Table(title="Family Media Scope Policy — Report Only")
    table.add_column("Scope status")
    table.add_column("Files", justify="right")
    table.add_column("Size", justify="right")
    for status, count in by_status.most_common():
        table.add_row(status, f"{count:,}", format_size(bytes_by_status[status]))
    console.print(table)
    console.print(f"[green]Local policy:[/green] {policy_path}")
    console.print(f"[green]Scope map:[/green] {scope_csv_path}")
    console.print(f"[green]Policy report:[/green] {report_path}")
    console.print("[dim]No source file was copied, moved, renamed, or deleted.[/dim]")

    return scope_csv_path, report_path, dict(by_status)
