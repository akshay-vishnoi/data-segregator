from __future__ import annotations

import typer

from .review_pack_v2 import build_pre_copy_review_pack


def main(
    project_name: str = typer.Argument(..., help="Existing media project name."),
) -> None:
    """Create corrected local pre-copy review CSVs from the dry-run plan only."""
    build_pre_copy_review_pack(project_name)


if __name__ == "__main__":
    typer.run(main)
