from __future__ import annotations

import csv
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .project import project_dir, require_project
from .sidecar_mapping import build_sidecar_mapping_report
from .utils import format_size, paths_overlap, resolved

console = Console()
SAFE_STATUS = "safe-one-to-one-match"
SIDECAREXT = {".aae", ".xmp", ".thm"}
SUCCESS = {"copied-and-verified", "resumed-and-verified", "already-verified"}
SELECTION_FIELDS = [
    "sidecar_plan_id", "sidecar_source_absolute_path", "sidecar_size_bytes",
    "sidecar_sha256", "matched_media_plan_id", "matched_media_destination",
    "planned_sidecar_destination", "destination_state",
]
AUDIT_FIELDS = [
    "timestamp_utc", "sidecar_plan_id", "sidecar_source_absolute_path",
    "planned_sidecar_destination", "size_bytes", "source_sha256",
    "destination_sha256", "status", "detail",
]


class SafeSidecarCopyError(RuntimeError):
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
    path = project_dir(project_name) / "sidecar_copy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SafeSidecarCopyError(f"Required report is missing: {path}")
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def _append_audit(path: Path, row: dict[str, str]) -> None:
    header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_FIELDS, extrasaction="ignore")
        if header:
            writer.writeheader()
        writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())


def _safe_child(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise SafeSidecarCopyError(f"Unsafe destination path: {relative_path!r}")
    target = root / relative
    if os.path.commonpath([str(root.resolve()), str(target.resolve(strict=False))]) != str(root.resolve()):
        raise SafeSidecarCopyError(f"Destination path escapes Family Media: {relative_path!r}")
    return target


def _destination_and_sources(project: dict, destination: str, same_drive_ok: bool) -> tuple[Path, dict[str, Path]]:
    root = resolved(destination)
    if root.name != "Family Media" or not root.exists() or not root.is_dir() or root.is_symlink():
        raise SafeSidecarCopyError("Destination must be the existing non-symlink Family Media folder.")
    sources: dict[str, Path] = {}
    same_filesystem = False
    for item in project["sources"]:
        source = resolved(item["path"])
        if not source.exists() or not source.is_dir() or source.is_symlink():
            raise SafeSidecarCopyError(f"Registered source is unavailable: {source}")
        if paths_overlap(source, root):
            raise SafeSidecarCopyError("Destination overlaps source. Refusing sidecar copy.")
        sources[item["id"]] = source
        same_filesystem = same_filesystem or os.stat(source).st_dev == os.stat(root).st_dev
    if same_filesystem and not same_drive_ok:
        raise SafeSidecarCopyError("Source and destination share a filesystem. Re-run with --same-drive-ok after acknowledging this is organization, not backup.")
    return root, sources


def _plan_index(project_name: str) -> dict[str, dict[str, str]]:
    plan_path = project_dir(project_name) / "plans" / "destination_dry_run_plan.csv"
    rows = _read_csv(plan_path)
    return {row["plan_id"]: row for row in rows if row.get("plan_id")}


def _mapping_rows(project_name: str) -> list[dict[str, str]]:
    build_sidecar_mapping_report(project_name)
    safe_path = project_dir(project_name) / "reviews" / "sidecar_mapping" / "sidecar_mapping_safe_one_to_one.csv"
    rows = _read_csv(safe_path)
    safe = [row for row in rows if row.get("mapping_status") == SAFE_STATUS]
    if not safe:
        raise SafeSidecarCopyError("No safe sidecar mappings are available.")
    return safe


def _source_path(row: dict[str, str], plan: dict[str, str], roots: dict[str, Path]) -> Path:
    if plan.get("media_type") != "sidecar" or plan.get("planned_action") != "preserve-review-sidecar":
        raise SafeSidecarCopyError("Sidecar mapping does not point to an active sidecar plan row.")
    if plan.get("source_absolute_path") != row.get("sidecar_source_absolute_path"):
        raise SafeSidecarCopyError("Sidecar mapping source no longer matches the current plan.")
    if plan.get("extension", "").lower() not in SIDECAREXT or not plan.get("sha256"):
        raise SafeSidecarCopyError("Sidecar plan row has unsupported extension or missing SHA-256.")
    root = roots.get(plan.get("source_id", ""))
    source = Path(plan["source_absolute_path"])
    if root is None or os.path.commonpath([str(root.resolve()), str(source.resolve(strict=False))]) != str(root.resolve()):
        raise SafeSidecarCopyError("Sidecar source escapes its registered source root.")
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise SafeSidecarCopyError(f"Sidecar source is unavailable or unsafe: {source}")
    if source.stat().st_size != int(plan["size_bytes"]):
        raise SafeSidecarCopyError(f"Sidecar source size changed since the plan: {source}")
    return source


def _validate_pair(row: dict[str, str], plans: dict[str, dict[str, str]], root: Path, roots: dict[str, Path]) -> tuple[Path, Path, str]:
    sidecar_plan = plans.get(row.get("sidecar_plan_id", ""))
    media_plan = plans.get(row.get("matched_media_plan_id", ""))
    if sidecar_plan is None or media_plan is None:
        raise SafeSidecarCopyError("Safe mapping references a plan row that is no longer present.")
    if media_plan.get("planned_action") not in {"planned-copy", "planned-copy-review"}:
        raise SafeSidecarCopyError("Matched media is no longer an approved copy row.")
    if media_plan.get("planned_destination_relative_path") != row.get("matched_media_destination"):
        raise SafeSidecarCopyError("Safe mapping media destination no longer matches the current plan.")
    source = _source_path(row, sidecar_plan, roots)
    media_target = _safe_child(root, media_plan["planned_destination_relative_path"])
    target = _safe_child(root, row.get("planned_sidecar_destination", ""))
    if target.parent != media_target.parent:
        raise SafeSidecarCopyError("Sidecar destination is not beside its matched media destination.")
    if not media_target.exists() or not media_target.is_file() or media_target.is_symlink():
        raise SafeSidecarCopyError(f"Matched media destination is missing or unsafe: {media_target}")
    if media_target.stat().st_size != int(media_plan["size_bytes"]):
        raise SafeSidecarCopyError(f"Matched media size differs from the approved plan: {media_target}")
    if not media_plan.get("sha256") or _hash(media_target).casefold() != media_plan["sha256"].casefold():
        raise SafeSidecarCopyError(f"Matched media SHA-256 differs from the approved plan: {media_target}")
    source_hash = _hash(source)
    if source_hash.casefold() != sidecar_plan["sha256"].casefold():
        raise SafeSidecarCopyError(f"Sidecar source SHA-256 differs from the approved plan: {source}")
    return source, target, source_hash


def _partial_path(target: Path, plan_id: str) -> Path:
    return target.with_name(f".{target.name}.sidecar-partial-{plan_id}")


def _copy_or_verify(source: Path, target: Path, plan_id: str, source_hash: str) -> tuple[str, str]:
    size = source.stat().st_size
    if target.exists():
        if target.is_symlink() or not target.is_file() or target.stat().st_size != size:
            raise SafeSidecarCopyError(f"Sidecar destination already exists and is not the expected file: {target}")
        destination_hash = _hash(target)
        if destination_hash != source_hash:
            raise SafeSidecarCopyError(f"Existing sidecar destination hash differs from its source: {target}")
        return "already-verified", destination_hash

    target.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(target, plan_id)
    offset = 0
    resumed = partial.exists()
    if resumed:
        if partial.is_symlink() or not partial.is_file() or partial.stat().st_size > size:
            raise SafeSidecarCopyError(f"Existing sidecar partial is unsafe: {partial}")
        offset = partial.stat().st_size
        with source.open("rb") as source_handle, partial.open("rb") as partial_handle:
            remaining = offset
            while remaining:
                block = source_handle.read(min(8 * 1024 * 1024, remaining))
                if not block or partial_handle.read(len(block)) != block:
                    raise SafeSidecarCopyError(f"Existing sidecar partial does not match its source prefix: {partial}")
                remaining -= len(block)
    mode = "ab" if resumed else "xb"
    with source.open("rb") as source_handle:
        source_handle.seek(offset)
        with partial.open(mode) as target_handle:
            while block := source_handle.read(8 * 1024 * 1024):
                target_handle.write(block)
            target_handle.flush()
            os.fsync(target_handle.fileno())
    if partial.stat().st_size != size:
        raise SafeSidecarCopyError(f"Sidecar partial size mismatch: {partial}")
    destination_hash = _hash(partial)
    if destination_hash != source_hash:
        raise SafeSidecarCopyError(f"Sidecar copy hash mismatch: {partial}")
    if target.exists():
        raise SafeSidecarCopyError(f"Destination appeared during sidecar copy; no overwrite occurred: {target}")
    partial.rename(target)
    return ("resumed-and-verified" if resumed else "copied-and-verified"), destination_hash


def _audit_row(row: dict[str, str], status: str, detail: str, source_hash: str = "", destination_hash: str = "") -> dict[str, str]:
    return {
        "timestamp_utc": _now(), "sidecar_plan_id": row["sidecar_plan_id"],
        "sidecar_source_absolute_path": row["sidecar_source_absolute_path"],
        "planned_sidecar_destination": row["planned_sidecar_destination"],
        "size_bytes": row["sidecar_size_bytes"], "source_sha256": source_hash,
        "destination_sha256": destination_hash, "status": status, "detail": detail,
    }


def run_safe_sidecar_copy(project_name: str, *, destination: str, apply: bool, same_drive_ok: bool, confirm_count: int | None) -> dict[str, int]:
    """Copy only freshly mapped, one-to-one, hash-verified sidecars beside verified media."""
    project = require_project(project_name)
    root, roots = _destination_and_sources(project, destination, same_drive_ok)
    plans = _plan_index(project_name)
    mappings = _mapping_rows(project_name)
    runtime = _runtime(project_name)
    selection_path = runtime / "safe_sidecar_selection.csv"
    audit_path = runtime / "safe_sidecar_copy_audit.csv"

    selection: list[dict[str, str]] = []
    existing = 0
    total_bytes = 0
    for row in mappings:
        source, target, source_hash = _validate_pair(row, plans, root, roots)
        state = "missing"
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.stat().st_size != source.stat().st_size or _hash(target) != source_hash:
                raise SafeSidecarCopyError(f"Existing safe-sidecar destination is not an exact verified match: {target}")
            state = "already-verified"
            existing += 1
        total_bytes += source.stat().st_size
        selection.append({
            "sidecar_plan_id": row["sidecar_plan_id"],
            "sidecar_source_absolute_path": str(source),
            "sidecar_size_bytes": str(source.stat().st_size),
            "sidecar_sha256": source_hash,
            "matched_media_plan_id": row["matched_media_plan_id"],
            "matched_media_destination": row["matched_media_destination"],
            "planned_sidecar_destination": row["planned_sidecar_destination"],
            "destination_state": state,
        })
    _write_csv(selection_path, SELECTION_FIELDS, selection)

    table = Table(title="Safe Sidecar Copy — Fresh Mapping and Verification")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Safe sidecars eligible", f"{len(mappings):,}")
    table.add_row("Safe sidecar size", format_size(total_bytes))
    table.add_row("Already verified at destination", f"{existing:,}")
    table.add_row("Need copy", f"{len(mappings) - existing:,}")
    table.add_row("Destination", str(root))
    console.print(table)
    console.print(f"[green]Verified sidecar selection:[/green] {selection_path}")

    if not apply:
        console.print("[yellow]Plan only. No source or destination file was changed. Re-run with --apply --confirm-count " + str(len(mappings)) + " to copy only these safe sidecars.[/yellow]")
        return {"eligible": len(mappings), "copied": 0, "already_verified": existing}
    if confirm_count != len(mappings):
        raise SafeSidecarCopyError(f"Confirmation count must equal the current safe sidecar count ({len(mappings)}). No file was copied.")

    copied = resumed = already_verified = 0
    for index, row in enumerate(mappings, start=1):
        try:
            source, target, source_hash = _validate_pair(row, plans, root, roots)
            status, destination_hash = _copy_or_verify(source, target, row["sidecar_plan_id"], source_hash)
            if status == "copied-and-verified":
                copied += 1
            elif status == "resumed-and-verified":
                resumed += 1
            else:
                already_verified += 1
            _append_audit(audit_path, _audit_row(row, status, "Sidecar source, matched media, and destination SHA-256 verification passed; source was not modified.", source_hash, destination_hash))
            if index % 100 == 0 or index == len(mappings):
                console.print(f"Completed {index:,}/{len(mappings):,} safe sidecars.")
        except Exception as error:
            _append_audit(audit_path, _audit_row(row, "failed", str(error)))
            raise SafeSidecarCopyError("Sidecar copy stopped safely. Later rows were not started; rerun the same command to resume. " + str(error)) from error

    console.print(f"[green]Sidecar copy audit:[/green] {audit_path}")
    console.print("[yellow]Original source sidecars were never changed. The 959 held-for-review sidecars remain at source only.[/yellow]")
    return {"eligible": len(mappings), "copied": copied, "resumed": resumed, "already_verified": already_verified}
