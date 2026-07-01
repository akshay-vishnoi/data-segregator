from __future__ import annotations

import typer

from .destination_plan import DestinationPlanError
from .destination_plan_finalize import finalize_destination_dry_run_plan


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
) -> None:
    """Finalize the report from an already completed destination dry-run CSV."""
    try:
        finalize_destination_dry_run_plan(project_name)
    except DestinationPlanError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
