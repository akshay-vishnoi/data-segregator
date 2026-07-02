from __future__ import annotations

import os
from pathlib import Path

from . import safe_sidecar_copy as base


class CurrentPairValidationError(base.SafeSidecarCopyError):
    pass


def _registered_file(plan: dict[str, str], roots: dict[str, Path], label: str) -> Path:
    root = roots.get(plan.get("source_id", ""))
    source_text = plan.get("source_absolute_path", "")
    source = Path(source_text)
    if root is None or not source_text:
        raise CurrentPairValidationError(f"{label} does not have a registered source root.")
    if os.path.commonpath([str(root.resolve()), str(source.resolve(strict=False))]) != str(root.resolve()):
        raise CurrentPairValidationError(f"{label} source is outside its registered source root.")
    if not source.exists() or not source.is_file() or source.is_symlink():
        raise CurrentPairValidationError(f"{label} source is unavailable or unsafe: {source}")
    return source


def validate_current_pair(
    mapping: dict[str, str],
    plans: dict[str, dict[str, str]],
    destination_root: Path,
    source_roots: dict[str, Path],
) -> tuple[Path, Path, str]:
    """Return a sidecar only after its current media source/destination pair matches.

    This intentionally compares current media bytes with each other, rather than
    requiring a historical plan hash that was shown to be stale for some pairs.
    """
    sidecar_plan = plans.get(mapping.get("sidecar_plan_id", ""))
    media_plan = plans.get(mapping.get("matched_media_plan_id", ""))
    if sidecar_plan is None or media_plan is None:
        raise CurrentPairValidationError("A safe mapping references a missing current plan row.")
    if sidecar_plan.get("media_type") != "sidecar" or sidecar_plan.get("planned_action") != "preserve-review-sidecar":
        raise CurrentPairValidationError("Mapping no longer points to an active sidecar row.")
    if sidecar_plan.get("source_absolute_path") != mapping.get("sidecar_source_absolute_path"):
        raise CurrentPairValidationError("Sidecar mapping source differs from the current plan.")
    if sidecar_plan.get("extension", "").lower() not in base.SIDECAREXT:
        raise CurrentPairValidationError("Unsupported sidecar extension.")
    if media_plan.get("media_type") not in {"photo", "video"}:
        raise CurrentPairValidationError("Matched item is no longer photo or video media.")
    if media_plan.get("planned_action") not in {"planned-copy", "planned-copy-review"}:
        raise CurrentPairValidationError("Matched media is no longer an approved copy row.")
    if media_plan.get("planned_destination_relative_path") != mapping.get("matched_media_destination"):
        raise CurrentPairValidationError("Matched media destination differs from the current plan.")

    sidecar_source = _registered_file(sidecar_plan, source_roots, "Sidecar")
    media_source = _registered_file(media_plan, source_roots, "Matched media")
    media_destination = base._safe_child(destination_root, media_plan["planned_destination_relative_path"])
    sidecar_destination = base._safe_child(destination_root, mapping.get("planned_sidecar_destination", ""))
    if sidecar_destination.parent != media_destination.parent:
        raise CurrentPairValidationError("Sidecar destination is not beside matched media.")
    if not media_destination.exists() or not media_destination.is_file() or media_destination.is_symlink():
        raise CurrentPairValidationError(f"Matched media destination is missing or unsafe: {media_destination}")
    if media_source.stat().st_size != media_destination.stat().st_size:
        raise CurrentPairValidationError(f"Current matched media sizes differ: {media_destination}")
    if base._hash(media_source).casefold() != base._hash(media_destination).casefold():
        raise CurrentPairValidationError(f"Current matched media hashes differ: {media_destination}")

    return sidecar_source, sidecar_destination, base._hash(sidecar_source)


def run_safe_sidecar_copy(
    project_name: str,
    *,
    destination: str,
    apply: bool,
    same_drive_ok: bool,
    confirm_count: int | None,
) -> dict[str, int]:
    """Copy only safe sidecars beside current source/destination-matched media."""
    base._validate_pair = validate_current_pair
    return base.run_safe_sidecar_copy(
        project_name,
        destination=destination,
        apply=apply,
        same_drive_ok=same_drive_ok,
        confirm_count=confirm_count,
    )
