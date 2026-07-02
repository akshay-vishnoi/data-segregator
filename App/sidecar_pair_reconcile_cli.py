from __future__ import annotations

import typer

from .sidecar_pair_reconcile import SidecarPairReconcileError, run_sidecar_pair_reconciliation


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
    destination: str = typer.Option(..., "--destination", help="Existing Family Media folder."),
    progress_every: int = typer.Option(50, "--progress-every", help="Show progress after this many safe sidecar/media pairs."),
) -> None:
    """Report-only reconciliation of safe sidecar mappings against source, destination, plan, and copy audit."""
    try:
        run_sidecar_pair_reconciliation(
            project_name,
            destination=destination,
            progress_every=progress_every,
        )
    except SidecarPairReconcileError as error:
        raise typer.BadParameter(str(error)) from error


if __name__ == "__main__":
    typer.run(main)
