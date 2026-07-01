from __future__ import annotations

import typer

from .full_copy import FullCopyError, run_full_copy


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    apply: bool = typer.Option(False, "--apply", help="Copy only the selected bounded batch."),
    same_drive_ok: bool = typer.Option(False, "--same-drive-ok", help="Acknowledge same-drive organization is not backup."),
    confirm_count: int | None = typer.Option(None, "--confirm-count", help="Must equal the exact current selected batch count."),
    max_files: int = typer.Option(500, "--max-files", help="Maximum files in this batch."),
    max_total_gb: float = typer.Option(25, "--max-total-gb", help="Maximum combined batch size in GiB."),
) -> None:
    """Plan or run one resumable, hash-verified family-media copy batch."""
    try:
        run_full_copy(
            project_name,
            destination=destination,
            apply=apply,
            same_drive_ok=same_drive_ok,
            confirm_count=confirm_count,
            max_files=max_files,
            max_total_gb=max_total_gb,
        )
    except FullCopyError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
