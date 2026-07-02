# Data Segregator — Media Separation Project Template

## Current Goal

Separate personal photos and videos from a mixed archive without disturbing documents, projects, source code, downloads, or other non-media files.

## Project model

A **project** is a long-lived media catalog, for example:

```text
family-media
```

A project can accept multiple sources over time:

```text
primary-archive
old-phone-backup
partner-old-hdd
future-cloud-export
```

Each source gets its own manifest and checkpoint. All sources feed the same catalog and eventual `Media_Organized` destination.

## Current project status — family-media-master

Completed:

- 60,319 selected media files / 1.20 TB copied into `Family Media` with source-to-destination SHA-256 verification.
- 789 safe one-to-one sidecars copied beside their matched media with SHA-256 verification.
- Original `Data` source was never changed.
- 959 uncertain sidecars remain preserved in the original source only.

### Open TODO — second physical-drive backup

- Purchase and prepare a separate 8 TB external HDD.
- Back up both the original `Data` archive (~1.4 TB) and the organized `Family Media` library (~1.2 TB).
- Do not delete, move, or rename anything from the original source until that second-drive backup and the Immich trial are complete.

### Active next phase — Immich trial

- Run Immich against `Family Media` as a read-only external library.
- Keep `Family Media` unchanged while evaluating the timeline, dates, folder view, duplicates, and missing media over a one-month trial.

## Planned phases

1. Durable per-source media inventory
2. SQLite catalog and inventory reports
3. Metadata extraction (EXIF/video capture date, duration, resolution, device)
4. Exact duplicate grouping via hashes
5. Review queue for candidates and likely irrelevant media
6. Create an organized media copy by capture year/month
7. Verify source-to-target copies
8. Trial the final organized library with Immich for one month
9. Create a reviewed deletion manifest for source media only
10. Delete source media only after a second physical backup exists

## Out of scope for the current Media Separation project

- Organizing documents
- Deleting project files or source code
- Deleting non-media from source folders
- Automatic deletion of photos/videos
- Treating Immich as the only backup

## Required safety gates before source media deletion

- Exported media is verified against source records.
- A second physical copy of the final media library exists.
- Immich trial succeeds.
- The deletion manifest is reviewed explicitly.
- Only source media paths are included; non-media stays intact.
