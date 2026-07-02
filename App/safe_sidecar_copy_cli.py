from __future__ import annotations

import typer

from .safe_sidecar_copy import SafeSidecarCopyError, run_safe_sidecar_copy


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    apply: bool = typer.Option(False, "--apply", help="Copy only freshly mapped safe sidecars."),
    same_drive_ok: bool = typer.Option(False, "--same-drive-ok", help="Acknowledge same-drive organization is not backup."),
    confirm_count: int | None = typer.Option(None, "--confirm-count", help="Must equal the exact current safe-sidecar count."),
) -> None:
    """Plan or copy only one-to-one, hash-verified sidecars beside verified media."""
    try:
        run_safe_sidecar_copy(
            project_name,
            destination=destination,
            apply=apply,
            same_drive_ok=same_drive_ok,
            confirm_count=confirm_count,
        )
    except SafeSidecarCopyError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
