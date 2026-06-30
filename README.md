# Data Segregator

A **safe, local-first, resumable** toolkit for separating useful data from mixed archives without damaging the original source.

The first implemented module is **Media Separation**: it inventories photos, videos, media companions, and review candidates from a mixed archive containing documents, projects, downloads, code, and other files.

Future modules may handle documents, project files, downloads, or other data types. Each module should use separate projects and rules rather than treating every file type with the same workflow.

This repository stores only code and documentation. Personal paths, manifests, databases, reports, logs, and media live under `runtime/`, which is ignored by Git.

## Naming model

- **Application / Git repository:** `data-segregator`
- **Project:** a long-lived catalog for one data domain, for example `family-media`
- **Source:** one import batch, for example `primary-archive` or `old-phone-backup`

Example future projects:

```text
family-media
documents-archive
project-archives
```

## Safety model

- Source folders are treated as **read-only**.
- The current version does **not** move, rename, copy, delete, repair, or modify source files.
- Runtime state is stored on internal storage, not beside the source data.
- Any future export must use a separately configured destination and be verified before source files are deleted.
- No deletion is automatic.

## Current capability: Media Separation

- Create a named long-lived project.
- Store project-specific runtime state locally under `runtime/<project-name>/`.
- Register multiple import sources over time.
- Scan one source at a time and save a durable media manifest.
- Resume an interrupted scan safely.
- Index all source manifests into SQLite.
- Generate inventory reports.
- Hash files for exact duplicate analysis and generate duplicate reports.

## Not implemented yet

- Read EXIF/video capture metadata.
- Organize files by year/month.
- Copy files into the final organized destination.
- Detect visually similar media.
- Delete duplicates or irrelevant media.
- Import into Immich.
- Non-media separation modules.

Those phases will be added only after the inventory has been reviewed.

## File layout

```text
data-segregator/                 # Internal Mac storage + Git repository
├── App/                          # Python application
├── tests/
├── runtime/                      # Git-ignored local state
│   └── family-media/
│       ├── project.json
│       ├── database/media.sqlite
│       ├── sources/<source-label>/manifest.csv
│       ├── reports/
│       └── logs/
├── README.md
├── PROJECT.md
├── DECISIONS.md
├── ARCHITECTURE.md
├── TODO.md
└── .gitignore
```

Mixed source data and the eventual destination live outside this repository, generally on external storage.

## Setup on macOS

```bash
cd ~/Developer/data-segregator
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
python3 -m pip install -e .
```

You can then use `data-segregator ...` while the virtual environment is active. Alternatively, use `python3 -m App ...`.

## Example workflow — Media Separation

Create a permanent catalog project and specify the eventual organized-media destination:

```bash
data-segregator project create family-media \
  --destination "/Volumes/DESTINATION_DRIVE/Media_Organized"
```

Add the first mixed source:

```bash
data-segregator source add family-media \
  --label "primary-archive" \
  --path "/Volumes/SOURCE_DRIVE/Data"
```

Scan that source (read-only):

```bash
data-segregator scan family-media --source primary-archive
```

Build the SQLite catalog and reports:

```bash
data-segregator index family-media
data-segregator report family-media
```

Later, add a newly discovered source to the same project:

```bash
data-segregator source add family-media \
  --label "old-phone-backup" \
  --path "/Volumes/PHONE_BACKUP/DCIM"

data-segregator scan family-media --source old-phone-backup
data-segregator index family-media
```

## Media classification

### Definite photos

`jpg`, `jpeg`, `heic`, `heif`, `png`, `gif`, `dng`, `cr2`, `nef`, `arw`, `orf`, `rw2`, `pef`, `srw`

### Definite videos

`mp4`, `mov`, `m4v`, `avi`, `mkv`, `3gp`, `3g2`, `mts`, `m2ts`

### Candidate media — retained for review

`dat`, `vob`, `ts`, `mpg`, `mpeg`, `wmv`, `flv`, `asf`, `dv`, `hevc`, `webm`, `webp`, `avif`, `bmp`, `tif`, `tiff`, `jfif`, `raw`

`.dat` is deliberately retained as a **candidate video** because older VCD/camera videos may use this extension. It is never silently discarded.

### Media companions

`aae`, `xmp`, `thm`, and Google Takeout JSON sidecars are captured as companions for later pairing with their parent media.

## Important notes

- A resumed scan may traverse a source tree again to find current files, but already written manifest records are not duplicated.
- Adding a new source scans only that new source. It does not reprocess old source manifests, metadata, or hashes.
- Exact duplicate handling uses content hashes and never deletes a source copy.
