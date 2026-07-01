from __future__ import annotations

import typer

from .canonical import generate_canonical_copy_proposal, show_canonical_copy_proposal_summary


def main(project_name: str = typer.Argument(..., help="Project with completed exact-duplicate hashes.")) -> None:
    """Create review-only canonical-copy proposals for exact duplicate groups."""
    proposals_path, candidates_path, summary = generate_canonical_copy_proposal(project_name)
    show_canonical_copy_proposal_summary(summary, proposals_path, candidates_path)


if __name__ == "__main__":
    typer.run(main)
