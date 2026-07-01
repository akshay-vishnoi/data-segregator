from __future__ import annotations

import typer

from .metadata_safe import MetadataToolError, run_metadata_extraction


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    source: list[str] | None = typer.Option(None, "--source", help="Optional source label. Repeat to process selected sources."),
    limit: int | None = typer.Option(None, "--limit", help="Process only this many not-yet-recorded files for a safe sample run."),
    batch_size: int = typer.Option(500, "--batch-size", help="Files passed to ExifTool per read-only batch."),
) -> None:
    """Extract capture-date metadata into local reports without changing source files."""
    try:
        run_metadata_extraction(
            project_name,
            source_labels=set(source) if source else None,
            limit=limit,
            batch_size=batch_size,
        )
    except MetadataToolError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
