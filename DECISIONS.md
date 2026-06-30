# Data Segregator — Decisions

## Product naming

The reusable application is named `data-segregator`. Each user creates long-lived projects such as `family-media`, `documents-archive`, or `project-archives`. Different data domains should use separate modules with their own classification and safety rules.

## Media separation, not full archive cleanup

The first module handles media only. The original mixed data folder remains intact while documents, projects, code, and non-media are intentionally out of scope.

## Code on internal storage; data on external storage

The Git repository and Python application belong on the computer's internal disk. Runtime manifests, databases, logs, and reports also remain local. Source and final media folders generally stay on external storage.

## Project runtime is ignored by Git

All private paths and generated state live under `runtime/<project-name>/`. The entire `runtime/` folder is ignored by Git.

## Long-lived projects with incremental sources

A project is a persistent media catalog. New sources may be added months or years later under the same project name. Each source receives a separate manifest and checkpoint, while all media remains available to the same dedupe and eventual export workflow.

## Read-only source policy

Scanner, indexer, reporter, and hasher never change source files. Future export will copy to a new destination before any deletion is considered.

## Candidate formats are preserved

Ambiguous formats such as `.dat` are cataloged as candidate media. This protects older VCD/camera-style files.

## No automatic deletion

Python and later AI/Immich may suggest duplicates or irrelevant media. They never delete anything without explicit human approval and a verified backup.

## Immich is a trial and review layer

Immich will later index the verified organized filesystem read-only. The organized filesystem, not Immich's database, remains the source of truth.
