from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .utils import paths_overlap, resolved, slugify

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOT = REPO_ROOT / "runtime"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def project_dir(project_name: str) -> Path:
    return RUNTIME_ROOT / slugify(project_name)


def project_config_path(project_name: str) -> Path:
    return project_dir(project_name) / "project.json"


def require_project(project_name: str) -> dict:
    config_path = project_config_path(project_name)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Project '{slugify(project_name)}' does not exist. "
            "Create it with: family-media project create <name> --destination <path>"
        )
    return json.loads(config_path.read_text(encoding="utf-8"))


def save_project(project: dict) -> None:
    project_name = project["slug"]
    target = project_config_path(project_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(project, indent=2) + "\n", encoding="utf-8")
    temporary.replace(target)


def create_project(name: str, destination: str) -> dict:
    slug = slugify(name)
    root = project_dir(slug)
    config_path = root / "project.json"
    if config_path.exists():
        raise FileExistsError(f"Project '{slug}' already exists.")

    destination_path = resolved(destination)
    root.mkdir(parents=True, exist_ok=False)
    for child in ("database", "sources", "reports", "logs", "exports"):
        (root / child).mkdir(parents=True, exist_ok=True)

    project = {
        "schema_version": 1,
        "name": name,
        "slug": slug,
        "created_at": utc_now(),
        "destination_path": str(destination_path),
        "sources": [],
    }
    save_project(project)
    return project


def add_source(project_name: str, label: str, source_path: str) -> dict:
    project = require_project(project_name)
    safe_label = slugify(label)
    source = resolved(source_path)
    if not source.exists() or not source.is_dir():
        raise FileNotFoundError(f"Source folder does not exist or is not a folder: {source}")

    destination = resolved(project["destination_path"])
    if paths_overlap(source, destination):
        raise ValueError(
            "Source and destination overlap. Choose a separate final media destination."
        )

    if any(existing["label"] == safe_label for existing in project["sources"]):
        raise ValueError(f"A source with label '{safe_label}' already exists.")

    source_record = {
        "id": str(uuid.uuid4()),
        "label": safe_label,
        "path": str(source),
        "added_at": utc_now(),
        "scan_status": "not_started",
        "last_scan_at": None,
    }
    project["sources"].append(source_record)

    source_runtime = project_dir(project_name) / "sources" / safe_label
    source_runtime.mkdir(parents=True, exist_ok=True)
    save_project(project)
    return source_record


def get_source(project_name: str, label: str) -> tuple[dict, dict]:
    project = require_project(project_name)
    safe_label = slugify(label)
    for source in project["sources"]:
        if source["label"] == safe_label:
            return project, source
    raise KeyError(f"Source '{safe_label}' is not registered in project '{project['slug']}'.")


def relink_source(project_name: str, label: str, source_path: str) -> dict:
    project, source = get_source(project_name, label)
    new_path = resolved(source_path)
    if not new_path.exists() or not new_path.is_dir():
        raise FileNotFoundError(f"Source folder does not exist or is not a folder: {new_path}")

    destination = resolved(project["destination_path"])
    if paths_overlap(new_path, destination):
        raise ValueError("Source and destination overlap. Refusing to relink.")

    source["path"] = str(new_path)
    source["relinked_at"] = utc_now()
    save_project(project)
    return source


def source_runtime_dir(project_name: str, source_label: str) -> Path:
    return project_dir(project_name) / "sources" / slugify(source_label)


def update_source_status(project: dict, source_id: str, status: str) -> None:
    for source in project["sources"]:
        if source["id"] == source_id:
            source["scan_status"] = status
            source["last_scan_at"] = utc_now()
            save_project(project)
            return
    raise KeyError(f"Source ID '{source_id}' was not found.")


def same_filesystem_warning(source_path: str, destination_path: str) -> bool:
    try:
        return os.stat(source_path).st_dev == os.stat(destination_path).st_dev
    except OSError:
        return False
