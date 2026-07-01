from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path
from typing import Iterable

from rich.console import Console
from rich.table import Table

from .db import database_path, get_connection
from .project import require_project
from .reporting import report_dir
from .utils import format_size

console = Console()


@dataclass(frozen=True)
class DuplicateCandidate:
    sha256: str
    copies: int
    size_bytes: int
    source_label: str
    source_relative_path: str
    filename: str
    media_type: str
    confidence: str


EVENT_TERMS = (
    "birthday",
    "bday",
    "wedding",
    "anniversary",
    "vacation",
    "holiday",
    "trip",
    "festival",
    "diwali",
    "christmas",
    "eid",
    "party",
    "graduation",
    "recital",
    "school",
    "newborn",
    "baby",
)

# These tokens indicate a transfer, backup, or duplicate-oriented folder. They reduce
# preference only; they never mark a record for deletion.
AVOID_PATTERNS: tuple[tuple[str, int, str], ...] = (
    (r"(^|[ /_-])xfer([ /_-]|$)", -100, "transfer-folder"),
    (r"\btransfer\b", -80, "transfer-folder"),
    (r"\bbackup(s)?\b", -70, "backup-folder"),
    (r"\bcopy\b", -60, "copy-folder-or-name"),
    (r"\bdropbox\b", -45, "sync-folder"),
    (r"\barchive\b", -35, "archive-folder"),
    (r"\biphone data\b", -45, "phone-backup-folder"),
    (r"\bmobile data\b", -40, "phone-backup-folder"),
    (r"\bfull\b.*\bmac\b", -25, "full-device-backup-folder"),
    (r"\bmac[- ]?[a-z0-9]+\b", -15, "device-named-folder"),
    (r"(^|/)hdd(/|$)", -30, "disk-backup-folder"),
    (r"\bdesktop\b", -15, "desktop-folder"),
)


def _normalized_path(candidate: DuplicateCandidate) -> str:
    return candidate.source_relative_path.replace("\\", "/").casefold()


def _score_candidate(candidate: DuplicateCandidate) -> tuple[int, tuple[str, ...]]:
    path_text = _normalized_path(candidate)
    score = 0
    reasons: list[str] = []

    matched_events = [term for term in EVENT_TERMS if re.search(rf"\b{re.escape(term)}\b", path_text)]
    if matched_events:
        score += 70
        reasons.append("event-context:" + ",".join(matched_events))

    path_depth = len(Path(candidate.source_relative_path).parts)
    if path_depth >= 2:
        context_points = min(path_depth, 8)
        score += context_points
        reasons.append(f"folder-context:+{context_points}")

    for pattern, penalty, label in AVOID_PATTERNS:
        if re.search(pattern, path_text):
            score += penalty
            reasons.append(label)

    if re.search(r"(?:[-_ ]0{0,2}[2-9]|[-_ ]\d{3,})\.[^.]+$", candidate.filename.casefold()):
        score -= 10
        reasons.append("numbered-copy-suffix")

    if not reasons:
        reasons.append("no-path-signal")
    return score, tuple(reasons)


def _proposal_confidence(best_score: int, score_margin: int) -> str:
    if best_score > 0 and score_margin >= 25:
        return "stronger-proposal"
    if best_score > 0 and score_margin >= 10:
        return "review-recommended"
    return "manual-review-required"


def _candidate_query() -> str:
    return """
        SELECT
            media_files.sha256,
            duplicate_groups.copies,
            media_files.size_bytes,
            media_files.source_label,
            media_files.source_relative_path,
            media_files.filename,
            media_files.media_type,
            media_files.confidence
        FROM media_files
        JOIN (
            SELECT sha256, COUNT(*) AS copies
            FROM media_files
            WHERE sha256 IS NOT NULL
            GROUP BY sha256
            HAVING COUNT(*) > 1
        ) AS duplicate_groups
          ON duplicate_groups.sha256 = media_files.sha256
        ORDER BY media_files.sha256, media_files.source_label, media_files.source_relative_path
    """


def _rows_to_candidates(rows: Iterable[object]) -> list[DuplicateCandidate]:
    return [
        DuplicateCandidate(
            sha256=row["sha256"],
            copies=int(row["copies"]),
            size_bytes=int(row["size_bytes"]),
            source_label=row["source_label"],
            source_relative_path=row["source_relative_path"],
            filename=row["filename"],
            media_type=row["media_type"],
            confidence=row["confidence"],
        )
        for row in rows
    ]


def generate_canonical_copy_proposal(project_name: str) -> tuple[Path, Path, dict[str, int]]:
    """Create review-only canonical-copy proposals for exact duplicate groups."""
    require_project(project_name)
    if not database_path(project_name).exists():
        raise FileNotFoundError("Database not found. Run: data-segregator index <project>")

    proposals_path = report_dir(project_name) / "canonical_copy_proposals.csv"
    candidates_path = report_dir(project_name) / "canonical_copy_proposal_candidates.csv"
    conn = get_connection(project_name)

    proposal_fields = [
        "sha256",
        "copies",
        "size_bytes",
        "additional_copy_bytes",
        "media_type",
        "confidence",
        "proposal_confidence",
        "proposal_score",
        "score_margin",
        "proposal_reasons",
        "proposed_source_label",
        "proposed_source_relative_path",
        "proposed_filename",
    ]
    candidate_fields = [
        "sha256",
        "rank",
        "selected_for_review",
        "score",
        "score_delta_from_best",
        "score_reasons",
        "source_label",
        "source_relative_path",
        "filename",
        "media_type",
        "confidence",
        "size_bytes",
    ]

    summary = {
        "groups": 0,
        "stronger_proposals": 0,
        "review_recommended": 0,
        "manual_review_required": 0,
        "additional_copy_bytes": 0,
    }

    try:
        cursor = conn.execute(_candidate_query())
        with proposals_path.open("w", newline="", encoding="utf-8") as proposals_handle, candidates_path.open("w", newline="", encoding="utf-8") as candidates_handle:
            proposals_writer = csv.DictWriter(proposals_handle, fieldnames=proposal_fields)
            candidates_writer = csv.DictWriter(candidates_handle, fieldnames=candidate_fields)
            proposals_writer.writeheader()
            candidates_writer.writeheader()

            for _, grouped_rows in groupby(cursor, key=lambda row: row["sha256"]):
                candidates = _rows_to_candidates(grouped_rows)
                scored = []
                for candidate in candidates:
                    score, reasons = _score_candidate(candidate)
                    scored.append((candidate, score, reasons))
                scored.sort(key=lambda item: (-item[1], item[0].source_relative_path.casefold(), item[0].source_label.casefold()))

                selected, best_score, selected_reasons = scored[0]
                second_score = scored[1][1] if len(scored) > 1 else best_score
                score_margin = best_score - second_score
                confidence = _proposal_confidence(best_score, score_margin)
                additional_copy_bytes = (selected.copies - 1) * selected.size_bytes

                summary["groups"] += 1
                summary["additional_copy_bytes"] += additional_copy_bytes
                if confidence == "stronger-proposal":
                    summary["stronger_proposals"] += 1
                elif confidence == "review-recommended":
                    summary["review_recommended"] += 1
                else:
                    summary["manual_review_required"] += 1

                proposals_writer.writerow({
                    "sha256": selected.sha256,
                    "copies": selected.copies,
                    "size_bytes": selected.size_bytes,
                    "additional_copy_bytes": additional_copy_bytes,
                    "media_type": selected.media_type,
                    "confidence": selected.confidence,
                    "proposal_confidence": confidence,
                    "proposal_score": best_score,
                    "score_margin": score_margin,
                    "proposal_reasons": "; ".join(selected_reasons),
                    "proposed_source_label": selected.source_label,
                    "proposed_source_relative_path": selected.source_relative_path,
                    "proposed_filename": selected.filename,
                })

                for rank, (candidate, score, reasons) in enumerate(scored, start=1):
                    candidates_writer.writerow({
                        "sha256": candidate.sha256,
                        "rank": rank,
                        "selected_for_review": "yes" if rank == 1 else "no",
                        "score": score,
                        "score_delta_from_best": best_score - score,
                        "score_reasons": "; ".join(reasons),
                        "source_label": candidate.source_label,
                        "source_relative_path": candidate.source_relative_path,
                        "filename": candidate.filename,
                        "media_type": candidate.media_type,
                        "confidence": candidate.confidence,
                        "size_bytes": candidate.size_bytes,
                    })
    finally:
        conn.close()

    return proposals_path, candidates_path, summary


def show_canonical_copy_proposal_summary(summary: dict[str, int], proposals_path: Path, candidates_path: Path) -> None:
    table = Table(title="Canonical Copy Proposals — Review Only")
    table.add_column("Item")
    table.add_column("Value", justify="right")
    table.add_row("Duplicate groups", f"{summary['groups']:,}")
    table.add_row("Stronger proposals", f"{summary['stronger_proposals']:,}")
    table.add_row("Review recommended", f"{summary['review_recommended']:,}")
    table.add_row("Manual review required", f"{summary['manual_review_required']:,}")
    table.add_row("Potential additional-copy bytes", format_size(summary["additional_copy_bytes"]))
    console.print(table)
    console.print(f"[green]Proposal report created:[/green] {proposals_path}")
    console.print(f"[green]Candidate-detail report created:[/green] {candidates_path}")
    console.print("[yellow]These are path-based review suggestions only. No file is copied, moved, renamed, or deleted.[/yellow]")
