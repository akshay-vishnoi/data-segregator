# Architecture

## Product boundary

`data-segregator` is the reusable application. It should remain domain-neutral.

Each **project** owns one catalog, its sources, reports, runtime state, destination policy, and future export rules. Do not mix media, documents, code archives, and other data domains into one project unless they genuinely share the same classification and export rules.

## Current module

The first module is Media Separation. It has a strict source-read-only policy and focuses on:

1. inventory
2. metadata (future)
3. duplicate analysis
4. review queues
5. organized export (future)
6. verification

## Runtime state

```text
runtime/<project-name>/
├── project.json
├── database/
├── sources/<source-label>/
├── reports/
├── logs/
└── exports/
```

`runtime/` is ignored by Git because it may contain personal paths, filenames, media metadata, and reports.

## Incremental imports

A project can receive new sources months or years later. Each source has its own label, manifest, checkpoint, and history. The project catalog uses all import batches to detect duplicates and determine future exports.

## Safety gates

A future export/delete workflow must require:

1. source-to-target verification
2. a second physical backup
3. review of a deletion manifest
4. explicit user approval
