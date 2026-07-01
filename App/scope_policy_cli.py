from __future__ import annotations

import typer

from .scope_policy import ScopePolicyError, build_scope_policy


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    source: list[str] | None = typer.Option(None, "--source", help="Optional source label. Repeat to process selected sources."),
) -> None:
    """Build a local scope map; sources remain untouched."""
    try:
        build_scope_policy(
            project_name,
            source_labels=set(source) if source else None,
        )
    except ScopePolicyError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
