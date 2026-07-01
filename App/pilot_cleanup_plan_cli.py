from __future__ import annotations

import typer

from .pilot_cleanup_plan import PilotCleanupPlanError, build_pilot_cleanup_plan


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
) -> None:
    """Create a report-only cleanup plan for earlier pilot files now excluded by policy."""
    try:
        build_pilot_cleanup_plan(project_name, destination=destination)
    except PilotCleanupPlanError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
