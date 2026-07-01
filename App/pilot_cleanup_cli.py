from __future__ import annotations

import typer

from .pilot_cleanup import PilotCleanupError, run_pilot_cleanup


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    apply: bool = typer.Option(False, "--apply", help="Remove verified destination pilot copies."),
    confirm_count: int | None = typer.Option(None, "--confirm-count", help="Must equal the exact current cleanup candidate count."),
) -> None:
    """Remove only hash-verified destination pilot files that the current policy excludes."""
    try:
        run_pilot_cleanup(
            project_name,
            destination=destination,
            apply=apply,
            confirm_count=confirm_count,
        )
    except PilotCleanupError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
