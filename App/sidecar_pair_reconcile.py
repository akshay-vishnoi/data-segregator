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
from .sidecar_mapping import build_sidecar_mapping_report
from .utils import format_size, resolved

console = Console()
SUCCESS_STATUSES = {"copied-and-verified", "resumed-and-verified", "already-verified"}
REPORT_FIELDS = [
    "sidecar_plan_id",
    "sidecar_source_relative_path",
    "matched_media_plan_id",
    "matched_media_source_absolute_path",
    "matched_media_destination",
    "expected_media_size_bytes",
    "destination_media_size_bytes",
    "expected_plan_sha256",
    "current_source_sha256",
    "current_destination_sha256",
    "full_copy_audit_status",
    "full_copy_audit_source_sha256",
    "full_copy_audit_destination_sha256",
    "reconciliation_status",
    "detail",
]


class SidecarPairReconcileError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _runtime(project_name: str) -> Path:
    path = project_dir(project_name) / "reviews" / "sidecar_reconciliation"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SidecarPairReconcileError(f"Required file is missing: {path}")
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _safe_destination(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise SidecarPairReconcileError(f"Unsafe destination path: {relative_path!r}")
    target = root / relative
    if os.path.commonpath([str(root.resolve()), str(target.resolve(strict=False))]) != str(root.resolve()):
        raise SidecarPairReconcileError(f"Destination escapes Family Media: {relative_path!r}")
    return target


def _source_roots(project: dict) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    for source in project["sources"]:
        root = resolved(source["path"])
        if not root.exists() or not root.is_dir() or root.is_symlink():
            raise SidecarPairReconcileError(f"Registered source is unavailable: {root}")
        roots[source["id"]] = root
    return roots


def _safe_source(plan: dict[str, str], roots: dict[str, Path]) -> Path:
    root = roots.get(plan.get("source_id", ""))
    source = Path(plan["source_absolute_path"])
    if root is None or os.path.commonpath([str(root.resolve()), str(source.resolve(strict=False))]) != str(root.resolve()):
        raise SidecarPairReconcileError("Media source escapes its registered source root.")
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise SidecarPairReconcileError(f"Media source is unavailable or unsafe: {source}")
    return source


def _plan_rows(project_name: str) -> dict[str, dict[str, str]]:
    path = project_dir(project_name) / "plans" / "destination_dry_run_plan.csv"
    return {row["plan_id"]: row for row in _read_csv(path) if row.get("plan_id")}


def _safe_mappings(project_name: str) -> list[dict[str, str]]:
    build_sidecar_mapping_report(project_name)
    path = project_dir(project_name) / "reviews" / "sidecar_mapping" / "sidecar_mapping_safe_one_to_one.csv"
    rows = _read_csv(path)
    return [row for row in rows if row.get("mapping_status") == "safe-one-to-one-match"]


def _latest_successful_copy_audit(project_name: str) -> dict[str, dict[str, str]]:
    path = project_dir(project_name) / "full_copy" / "full_copy_audit.csv"
    if not path.exists():
        return {}
    latest: dict[str, dict[str, str]] = {}
    with path.open("r", newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") in SUCCESS_STATUSES and row.get("plan_id"):
                latest[row["plan_id"]] = row
    return latest


def _status(
    *,
    expected: str,
    source_hash: str,
    destination_hash: str,
    audit: dict[str, str] | None,
) -> tuple[str, str]:
    expected = expected.casefold()
    source_ok = source_hash.casefold() == expected
    destination_ok = destination_hash.casefold() == expected
    source_destination_ok = source_hash.casefold() == destination_hash.casefold()
    if audit is None:
        audit_ok = False
    else:
        audit_ok = (
            audit.get("source_sha256", "").casefold() == expected
            and audit.get("destination_sha256", "").casefold() == expected
        )

    if source_ok and destination_ok and audit_ok:
        return "reconciled", "Current source, destination, approved plan, and successful migration audit all agree."
    if source_ok and destination_ok and audit is None:
        return "current-files-match-plan-audit-missing", "Current source and destination match the approved plan, but no successful migration-audit record exists."
    if source_ok and destination_ok:
        return "current-files-match-plan-audit-inconsistent", "Current source and destination match the approved plan, but the recorded migration hashes differ or are incomplete."
    if source_ok and not destination_ok:
        return "destination-differs-source-and-plan", "Current source matches the approved plan; destination does not."
    if not source_ok and destination_ok:
        return "source-differs-plan-destination-still-plan", "Current destination matches the approved plan; source changed after planning or migration."
    if source_destination_ok:
        return "source-and-destination-match-each-other-but-not-plan", "Current source and destination agree with each other but differ from the approved-plan hash."
    return "source-destination-plan-disagreement", "Current source, destination, and approved plan do not all agree."


def _report_row(
    mapping: dict[str, str],
    media: dict[str, str] | None,
    *,
    source_hash: str = "",
    destination_hash: str = "",
    destination_size: str = "",
    audit: dict[str, str] | None = None,
    status: str,
    detail: str,
) -> dict[str, str]:
    return {
        "sidecar_plan_id": mapping.get("sidecar_plan_id", ""),
        "sidecar_source_relative_path": mapping.get("sidecar_source_relative_path", ""),
        "matched_media_plan_id": mapping.get("matched_media_plan_id", ""),
        "matched_media_source_absolute_path": media.get("source_absolute_path", "") if media else "",
        "matched_media_destination": mapping.get("matched_media_destination", ""),
        "expected_media_size_bytes": media.get("size_bytes", "") if media else "",
        "destination_media_size_bytes": destination_size,
        "expected_plan_sha256": media.get("sha256", "") if media else "",
        "current_source_sha256": source_hash,
        "current_destination_sha256": destination_hash,
        "full_copy_audit_status": audit.get("status", "missing") if audit else "missing",
        "full_copy_audit_source_sha256": audit.get("source_sha256", "") if audit else "",
        "full_copy_audit_destination_sha256": audit.get("destination_sha256", "") if audit else "",
        "reconciliation_status": status,
        "detail": detail,
    }


def _write_markdown(path: Path, rows: list[dict[str, str]]) -> None:
    counts = Counter(row["reconciliation_status"] for row in rows)
    reconciled = counts["reconciled"]
    issues = len(rows) - reconciled
    lines = [
        "# Safe Sidecar Pair Reconciliation", "",
        f"Generated: `{_now()}`", "",
        "## Safety", "",
        "- This diagnostic reads the current source media, destination media, approved plan, and migration audit only.",
        "- It does not copy, move, rename, or delete any source, destination, or sidecar file.",
        "- Sidecar copying remains blocked until the mismatches are understood.", "",
        "## Summary", "",
        f"- Safe sidecar/media pairs checked: **{len(rows):,}**",
        f"- Fully reconciled: **{reconciled:,}**",
        f"- Needs review: **{issues:,}**", "",
        "## Results", "", "| Status | Pairs |", "|---|---:|",
    ]
    for status, count in counts.most_common():
        lines.append(f"| {status} | {count:,} |")
    lines.extend([
        "", "## Interpretation", "",
        "- `reconciled` means the current source media, destination media, approved-plan hash, and successful copy-audit hashes all agree.",
        "- Any other status is report-only evidence for resolving the mismatch; no sidecar is copied by this command.",
    ])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_sidecar_pair_reconciliation(
    project_name: str,
    *,
    destination: str,
    progress_every: int = 50,
) -> dict[str, int]:
    """Diagnose each safe sidecar's matched media without changing any file."""
    if progress_every <= 0:
        raise SidecarPairReconcileError("--progress-every must be positive.")
    project = require_project(project_name)
    destination_root = resolved(destination)
    if destination_root.name != "Family Media" or not destination_root.exists() or not destination_root.is_dir() or destination_root.is_symlink():
        raise SidecarPairReconcileError("Destination must be the existing non-symlink Family Media folder.")

    roots = _source_roots(project)
    plans = _plan_rows(project_name)
    mappings = _safe_mappings(project_name)
    copy_audit = _latest_successful_copy_audit(project_name)
    hash_cache: dict[str, str] = {}
    results: list[dict[str, str]] = []

    def cached_hash(path: Path) -> str:
        key = str(path)
        if key not in hash_cache:
            hash_cache[key] = _hash(path)
        return hash_cache[key]

    for index, mapping in enumerate(mappings, start=1):
        media = plans.get(mapping.get("matched_media_plan_id", ""))
        try:
            if media is None:
                raise SidecarPairReconcileError("Matched media plan row is missing from the current destination plan.")
            if media.get("planned_destination_relative_path") != mapping.get("matched_media_destination"):
                raise SidecarPairReconcileError("Matched media destination no longer matches the current plan.")
            source = _safe_source(media, roots)
            target = _safe_destination(destination_root, media["planned_destination_relative_path"])
            if not target.exists() or not target.is_file() or target.is_symlink():
                raise SidecarPairReconcileError("Matched media destination is missing, not regular, or is a symlink.")
            source_hash = cached_hash(source)
            destination_hash = cached_hash(target)
            audit = copy_audit.get(media["plan_id"])
            status, detail = _status(
                expected=media.get("sha256", ""),
                source_hash=source_hash,
                destination_hash=destination_hash,
                audit=audit,
            )
            results.append(
                _report_row(
                    mapping,
                    media,
                    source_hash=source_hash,
                    destination_hash=destination_hash,
                    destination_size=str(target.stat().st_size),
                    audit=audit,
                    status=status,
                    detail=detail,
                )
            )
        except Exception as error:
            results.append(
                _report_row(
                    mapping,
                    media,
                    status="reconciliation-error",
                    detail=str(error),
                )
            )
        if index % progress_every == 0 or index == len(mappings):
            completed = len(results)
            reconciled = sum(row["reconciliation_status"] == "reconciled" for row in results)
            console.print(f"Checked {completed:,}/{len(mappings):,} pairs; reconciled {reconciled:,}.")

    runtime = _runtime(project_name)
    csv_path = runtime / "sidecar_pair_reconciliation.csv"
    report_path = runtime / "sidecar_pair_reconciliation.md"
    _write_csv(csv_path, results)
    _write_markdown(report_path, results)

    counts = Counter(row["reconciliation_status"] for row in results)
    table = Table(title="Safe Sidecar Pair Reconciliation — Report Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Safe pairs checked", f"{len(results):,}")
    table.add_row("Fully reconciled", f"{counts['reconciled']:,}")
    table.add_row("Need review", f"{len(results) - counts['reconciled']:,}")
    for status, count in counts.most_common():
        table.add_row(status, f"{count:,}")
    console.print(table)
    console.print(f"[green]Detailed reconciliation CSV:[/green] {csv_path}")
    console.print(f"[green]Reconciliation report:[/green] {report_path}")
    console.print("[yellow]No source, destination, or sidecar file was changed.[/yellow]")
    return {"checked": len(results), "reconciled": counts["reconciled"], "review": len(results) - counts["reconciled"]}
