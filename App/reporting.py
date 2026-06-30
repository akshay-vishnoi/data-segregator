from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .db import database_path, get_connection
from .project import project_dir, require_project
from .utils import format_size

console = Console()


def report_dir(project_name: str) -> Path:
    path = project_dir(project_name) / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def generate_inventory_report(project_name: str) -> Path:
    require_project(project_name)
    if not database_path(project_name).exists():
        raise FileNotFoundError("Database not found. Run: data-segregator index <project>")
    conn = get_connection(project_name)
    total = conn.execute("SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes FROM media_files").fetchone()
    type_rows = conn.execute("""SELECT media_type, confidence, COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS bytes
                                FROM media_files GROUP BY media_type, confidence ORDER BY count DESC""").fetchall()
    extension_rows = conn.execute("""SELECT extension, media_type, confidence, COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS bytes
                                     FROM media_files GROUP BY extension, media_type, confidence ORDER BY count DESC""").fetchall()
    source_rows = conn.execute("""SELECT source_label, COUNT(*) AS count, COALESCE(SUM(size_bytes),0) AS bytes
                                  FROM media_files GROUP BY source_label ORDER BY count DESC""").fetchall()
    conn.close()

    lines = [
        "# Media Inventory Report", "",
        f"Generated: `{datetime.now(timezone.utc).isoformat()}`", "",
        "## Total", "",
        f"- Records: **{total['count']:,}**",
        f"- Size: **{format_size(total['bytes'])}**", "",
        "## By Source", "", "| Source | Files | Size |", "|---|---:|---:|",
    ]
    lines.extend(f"| {row['source_label']} | {row['count']:,} | {format_size(row['bytes'])} |" for row in source_rows)
    lines.extend(["", "## By Type / Confidence", "", "| Type | Confidence | Files | Size |", "|---|---|---:|---:|"])
    lines.extend(f"| {row['media_type']} | {row['confidence']} | {row['count']:,} | {format_size(row['bytes'])} |" for row in type_rows)
    lines.extend(["", "## By Extension", "", "| Extension | Type | Confidence | Files | Size |", "|---|---|---|---:|---:|"])
    lines.extend(f"| {row['extension']} | {row['media_type']} | {row['confidence']} | {row['count']:,} | {format_size(row['bytes'])} |" for row in extension_rows)

    output = report_dir(project_name) / "media_inventory_report.md"
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def generate_exact_duplicate_report(project_name: str) -> Path:
    require_project(project_name)
    if not database_path(project_name).exists():
        raise FileNotFoundError("Database not found. Run: data-segregator index <project>")
    conn = get_connection(project_name)
    groups = conn.execute("""SELECT sha256, COUNT(*) AS copies, MIN(size_bytes) AS size_bytes
                             FROM media_files WHERE sha256 IS NOT NULL
                             GROUP BY sha256 HAVING COUNT(*) > 1 ORDER BY copies DESC, size_bytes DESC""").fetchall()
    output = report_dir(project_name) / "exact_duplicate_groups.csv"
    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sha256", "copies", "size_bytes", "source_label", "source_relative_path", "filename", "media_type", "confidence"])
        for group in groups:
            records = conn.execute("""SELECT source_label, source_relative_path, filename, media_type, confidence
                                      FROM media_files WHERE sha256=? ORDER BY size_bytes DESC, source_label, source_relative_path""", (group["sha256"],)).fetchall()
            for record in records:
                writer.writerow([group["sha256"], group["copies"], group["size_bytes"], record["source_label"], record["source_relative_path"], record["filename"], record["media_type"], record["confidence"]])
    conn.close()
    return output


def show_status(project_name: str) -> None:
    project = require_project(project_name)
    table = Table(title=f"Project: {project['slug']}")
    table.add_column("Item")
    table.add_column("Value")
    table.add_row("Destination", project.get("destination_path") or "Not configured")
    table.add_row("Registered sources", str(len(project["sources"])))
    table.add_row("Database", "Present" if database_path(project_name).exists() else "Not built")
    for source in project["sources"]:
        table.add_row(f"Source: {source['label']}", f"{source['scan_status']} — {source['path']}")
    console.print(table)
