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
