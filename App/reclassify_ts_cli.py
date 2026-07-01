from __future__ import annotations

import typer

from .reclassify_ts import (
    apply_ts_reclassification,
    build_ts_reclassification_plan,
    console,
    show_ts_reclassification_plan,
)


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    source: str = typer.Option(..., "--source", help="Source label whose completed manifest will be checked."),
    apply: bool = typer.Option(False, "--apply", help="Apply the reviewed reclassification to local runtime state."),
) -> None:
    """Separate TypeScript source files from genuine MPEG transport-stream videos."""
    plan, _ = build_ts_reclassification_plan(project_name, source)
    show_ts_reclassification_plan(plan)
    if not apply:
        console.print("Run again with --apply only after reviewing this plan.")
        return

    if plan.remove_nonmedia == 0:
        console.print("[green]Nothing to remove from the media manifest or catalog.[/green]")
        return

    applied_plan, backup_path, deleted_catalog_rows = apply_ts_reclassification(project_name, source)
    console.print("[bold green].ts reclassification applied.[/bold green]")
    console.print(f"Removed from media manifest: {applied_plan.remove_nonmedia:,} confirmed non-media records")
    console.print(f"Removed from local catalog: {deleted_catalog_rows:,} rows")
    console.print(f"Manifest backup: {backup_path}")
    console.print("Regenerate report, duplicates, and canonical proposals next. No source file was changed.")


if __name__ == "__main__":
    typer.run(main)
