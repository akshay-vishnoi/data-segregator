from __future__ import annotations

import typer

from .metadata_v2 import MetadataToolError, run_metadata_extraction


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    source: list[str] | None = typer.Option(None, "--source", help="Optional source label. Repeat to process selected sources."),
    path: list[str] | None = typer.Option(None, "--path", help="Exact source-relative path to validate. Repeat for multiple files."),
    limit: int | None = typer.Option(None, "--limit", help="Process only this many not-yet-recorded files."),
    batch_size: int = typer.Option(500, "--batch-size", help="Files passed to ExifTool per read-only batch."),
) -> None:
    """Extract capture dates with corrected ExifTool group and timezone handling."""
    try:
        run_metadata_extraction(
            project_name,
            source_labels=set(source) if source else None,
            paths=set(path) if path else None,
            limit=limit,
            batch_size=batch_size,
        )
    except MetadataToolError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
