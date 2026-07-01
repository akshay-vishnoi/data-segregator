from __future__ import annotations

import typer

from .full_copy import FullCopyError
from .full_copy_auto import run_full_copy_automatic


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    apply: bool = typer.Option(False, "--apply", help="Run through the full approved plan automatically."),
    same_drive_ok: bool = typer.Option(False, "--same-drive-ok", help="Acknowledge same-drive organization is not backup."),
    confirm_plan_files: int | None = typer.Option(None, "--confirm-plan-files", help="Must equal the exact approved plan file count."),
    checkpoint_files: int = typer.Option(500, "--checkpoint-files", help="Internal checkpoint size; the command continues automatically."),
    checkpoint_total_gb: float = typer.Option(25.0, "--checkpoint-total-gb", help="Internal checkpoint size; the command continues automatically."),
) -> None:
    """Plan or run the whole approved media migration with automatic safe resume."""
    try:
        run_full_copy_automatic(
            project_name,
            destination=destination,
            apply=apply,
            same_drive_ok=same_drive_ok,
            confirm_plan_files=confirm_plan_files,
            checkpoint_files=checkpoint_files,
            checkpoint_total_gb=checkpoint_total_gb,
        )
    except FullCopyError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
