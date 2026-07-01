from __future__ import annotations

import json

import typer

from . import destination_plan
from .destination_plan import DestinationPlanError
from .destination_plan_v2 import build_destination_dry_run_plan

destination_plan.json = json


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    source: list[str] | None = typer.Option(None, "--source", help="Optional source label. Repeat to plan selected sources."),
) -> None:
    """Create the destination plan without changing media files."""
    try:
        build_destination_dry_run_plan(
            project_name,
            source_labels=set(source) if source else None,
        )
    except DestinationPlanError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
