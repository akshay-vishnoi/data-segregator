from __future__ import annotations

import typer

from .sidecar_mapping import build_sidecar_mapping_report


def main(project_name: str = typer.Argument(..., help="Existing media project name.")) -> None:
    build_sidecar_mapping_report(project_name)


if __name__ == "__main__":
    typer.run(main)
