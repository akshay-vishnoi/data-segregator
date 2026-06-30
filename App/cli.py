from __future__ import annotations

import typer
from rich.console import Console

from .hashing import hash_exact_duplicates
from .indexer import build_index
from .project import (
    add_source,
    create_project,
    relink_source,
    require_project,
    same_filesystem_warning,
    set_destination,
)
from .reporting import generate_exact_duplicate_report, generate_inventory_report, show_status
from .scanner import scan_source

app = typer.Typer(help="Safe, resumable data-segregator tools. Current module: media.", no_args_is_help=True)
project_app = typer.Typer(help="Create and inspect long-lived media projects.", no_args_is_help=True)
source_app = typer.Typer(help="Register and relink incremental import sources.", no_args_is_help=True)
app.add_typer(project_app, name="project")
app.add_typer(source_app, name="source")
console = Console()


@project_app.command("create")
def project_create(
    name: str = typer.Argument(..., help="Long-lived catalog name, for example family-media."),
    destination: str | None = typer.Option(
        None,
        "--destination",
        help="Optional future organized-media destination. Configure it before export.",
    ),
) -> None:
    project = create_project(name, destination)
    console.print(f"[bold green]Project created:[/bold green] {project['slug']}")
    console.print(f"Runtime state: runtime/{project['slug']}/")
    if project["destination_path"]:
        console.print(f"Destination: {project['destination_path']}")
    else:
        console.print("Destination: not configured (not needed for inventory, scan, index, or reports).")
    console.print("Next: data-segregator source add <project> --label <label> --path <source>")


@project_app.command("set-destination")
def project_set_destination(
    name: str = typer.Argument(..., help="Existing project name."),
    path: str = typer.Option(..., "--path", help="Separate final organized-media destination."),
) -> None:
    project = set_destination(name, path)
    console.print(f"[bold green]Destination configured:[/bold green] {project['destination_path']}")


@project_app.command("info")
def project_info(name: str = typer.Argument(...)) -> None:
    show_status(name)


@source_app.command("add")
def source_add(
    project_name: str = typer.Argument(...),
    label: str = typer.Option(..., "--label", help="Unique import batch label."),
    path: str = typer.Option(..., "--path", help="Folder to scan read-only."),
) -> None:
    source = add_source(project_name, label, path)
    project = require_project(project_name)
    if same_filesystem_warning(source["path"], project.get("destination_path")):
        console.print("[yellow]Warning:[/yellow] source and destination appear to be on the same filesystem. This is not a backup.")
    console.print(f"[bold green]Source added:[/bold green] {source['label']}")
    console.print("Next: data-segregator scan <project> --source <label>")


@source_app.command("list")
def source_list(project_name: str = typer.Argument(...)) -> None:
    project = require_project(project_name)
    if not project["sources"]:
        console.print("No sources registered.")
        return
    for source in project["sources"]:
        console.print(f"- [bold]{source['label']}[/bold] | {source['scan_status']} | {source['path']}")


@source_app.command("relink")
def source_relink(
    project_name: str = typer.Argument(...),
    label: str = typer.Option(..., "--label"),
    path: str = typer.Option(..., "--path"),
) -> None:
    source = relink_source(project_name, label, path)
    console.print(f"[bold green]Source relinked:[/bold green] {source['label']} → {source['path']}")


@app.command("scan")
def scan(
    project_name: str = typer.Argument(...),
    source: str = typer.Option(..., "--source", help="Registered source label to scan."),
) -> None:
    """Create/resume a durable media manifest for exactly one source."""
    scan_source(project_name, source)


@app.command("index")
def index(project_name: str = typer.Argument(...)) -> None:
    """Build/update the SQLite catalog from saved manifests."""
    build_index(project_name)


@app.command("report")
def report(project_name: str = typer.Argument(...)) -> None:
    """Generate a readable inventory report from the SQLite catalog."""
    path = generate_inventory_report(project_name)
    console.print(f"[bold green]Inventory report created:[/bold green] {path}")


@app.command("hash")
def hash_media(
    project_name: str = typer.Argument(...),
    source: str | None = typer.Option(None, "--source", help="Optional source label; omit to hash all unhashed catalog rows."),
    limit: int | None = typer.Option(None, "--limit", help="Optional safe test limit."),
) -> None:
    """Calculate SHA-256 hashes for exact duplicate analysis. Read-only."""
    hash_exact_duplicates(project_name, source, limit)


@app.command("duplicates")
def duplicates(project_name: str = typer.Argument(...)) -> None:
    """Create a CSV report of exact duplicate hash groups. Never deletes files."""
    path = generate_exact_duplicate_report(project_name)
    console.print(f"[bold green]Exact duplicate report created:[/bold green] {path}")


@app.command("status")
def status(project_name: str = typer.Argument(...)) -> None:
    """Show project, source, and catalog status."""
    show_status(project_name)
