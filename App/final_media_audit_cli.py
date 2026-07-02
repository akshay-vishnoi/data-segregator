from __future__ import annotations

import typer

from .final_media_audit import FinalMediaAuditError, run_final_media_audit


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    progress_every: int = typer.Option(500, "--progress-every", help="Show progress after this many files."),
) -> None:
    """Re-hash every approved destination media file without changing data."""
    try:
        run_final_media_audit(
            project_name,
            destination=destination,
            progress_every=progress_every,
        )
    except FinalMediaAuditError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
