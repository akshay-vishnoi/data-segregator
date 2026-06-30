# Data Segregator — Decisions

## Product naming

The reusable application is named `data-segregator`. The current project remains `vishnoi-family-media`. Future data domains should be implemented as separate projects and modules with their own classification and safety rules.

## 2026-06 — Media separation, not full archive cleanup

This project handles family media only. The original mixed data folder remains intact while documents, projects, code, and non-media are intentionally out of scope.

## 2026-06 — Code on internal storage; data on external storage

The Git repository and Python application belong on the Mac's internal disk. Runtime manifests, database, logs, and reports also remain local. Source and final media folders stay on external storage.

## 2026-06 — Project runtime is ignored by Git

All private paths and generated state live under `runtime/<project-name>/`. The entire `runtime/` folder is ignored by Git.

## 2026-06 — Long-lived projects with incremental sources

A project is a persistent media catalog. New sources may be added months or years later under the same project name. Each source receives a separate manifest and checkpoint, while all media remains available to the same dedupe and eventual export workflow.

## 2026-06 — Read-only source policy

Scanner, indexer, reporter, and hasher never change source files. Future export will copy to a new destination before any deletion is considered.

## 2026-06 — Candidate formats are preserved

Ambiguous formats such as `.dat` are cataloged as candidate media. This protects old video formats, including a first-birthday video stored as `.dat`.

## 2026-06 — No automatic deletion

Python and later AI/Immich may suggest duplicates or irrelevant media. They never delete anything without explicit human approval and a verified backup.

## 2026-06 — Immich is a trial and review layer

Immich will later index the verified organized filesystem read-only. The organized filesystem, not Immich's database, remains the source of truth.
