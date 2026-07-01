from __future__ import annotations

import typer

from .pilot_copy import PilotCopyError, run_pilot_copy


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    apply: bool = typer.Option(False, "--apply", help="Actually copy the selected pilot files after source and destination hash verification."),
    same_drive_ok: bool = typer.Option(False, "--same-drive-ok", help="Acknowledge this is same-drive organization, not backup."),
    max_file_mb: int = typer.Option(1024, "--max-file-mb", help="Maximum size for a single pilot file."),
    max_total_gb: int = typer.Option(8, "--max-total-gb", help="Maximum combined pilot size."),
) -> None:
    """Build or run a small verified pilot copy. Sources are never changed."""
    try:
        run_pilot_copy(
            project_name,
            destination=destination,
            apply=apply,
            same_drive_ok=same_drive_ok,
            max_file_mb=max_file_mb,
            max_total_gb=max_total_gb,
        )
    except PilotCopyError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
