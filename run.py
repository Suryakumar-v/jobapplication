"""Command-line entry point: serve the API, initialise the database, validate configuration."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).resolve().parent))

import typer
import uvicorn
from rich.console import Console

from app import __version__
from app.config import load_settings

cli = typer.Typer(
    add_completion=False, help="Job application assistance system (preparation mode)."
)
console = Console()
MAX_IMPORT_FILE_BYTES = 5 * 1024 * 1024


@cli.command()
def serve(
    host: str | None = typer.Option(None, help="Override API_HOST."),
    port: int | None = typer.Option(None, help="Override API_PORT."),
) -> None:
    """Start the FastAPI service."""
    settings = load_settings()
    violations = settings.safety_violations()
    if violations:
        console.print("[red]Refusing to start:[/red] " + " ".join(violations))
        raise typer.Exit(code=1)
    uvicorn.run(
        "app.main:app_factory",
        factory=True,
        host=host or settings.api_host,
        port=port or settings.api_port,
    )


@cli.command("init-db")
def init_db() -> None:
    """Create directories and the SQLite database."""
    from scripts.initialize_database import main

    raise typer.Exit(code=main())


@cli.command("validate-config")
def validate_config() -> None:
    """Validate environment settings and YAML configuration."""
    from scripts.validate_configuration import main

    raise typer.Exit(code=main())


@cli.command("validate-workflows")
def validate_workflows() -> None:
    """Validate n8n workflow JSON files."""
    from scripts.validate_n8n_workflows import main

    raise typer.Exit(code=main([]))


@cli.command("import-jobs")
def import_jobs(
    path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="A .csv or .json file.")
    ],
) -> None:
    """Import jobs from a local CSV or JSON file into the tracker."""
    import json

    from app.database.migrations import initialize_database
    from app.database.session import create_db_engine, create_session_factory, session_scope
    from app.models.job import JobSource
    from app.services.job_importer import (
        ImportFormatError,
        JobIngestionService,
        parse_csv_text,
        parse_json_payload,
    )

    if path.stat().st_size > MAX_IMPORT_FILE_BYTES:
        console.print(f"[red]File larger than {MAX_IMPORT_FILE_BYTES} bytes.[/red]")
        raise typer.Exit(code=1)
    text = path.read_text(encoding="utf-8-sig")
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            records, source = parse_csv_text(text), JobSource.CSV
        elif suffix == ".json":
            records, source = parse_json_payload(json.loads(text)), JobSource.JSON
        else:
            console.print("[red]Only .csv and .json files are supported.[/red]")
            raise typer.Exit(code=1)
    except (ImportFormatError, json.JSONDecodeError) as exc:
        console.print(f"[red]Cannot parse file:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    settings = load_settings()
    engine = create_db_engine(settings.database_file)
    initialize_database(engine)
    try:
        with session_scope(create_session_factory(engine)) as session:
            summary = JobIngestionService(session, settings).ingest_records(records, source)
    finally:
        engine.dispose()
    console.print(
        f"received={summary.received} created={summary.created} duplicates={summary.duplicates} "
        f"rejected={summary.rejected} needs_extraction={summary.needs_extraction} "
        f"deferred={summary.deferred}"
    )
    for item in summary.results:
        if item.errors:
            console.print(f"  row {item.index}: {item.status.value}: {'; '.join(item.errors)}")


@cli.command("export")
def export_tracker(
    fmt: Annotated[
        str, typer.Option("--format", help="xlsx, csv or both.", case_sensitive=False)
    ] = "both",
) -> None:
    """Write exports\\applications.xlsx and/or applications.csv from the database."""
    from app.database.migrations import initialize_database
    from app.database.session import create_db_engine, create_session_factory, session_scope
    from app.services.export_service import (
        FORMATS,
        ExportError,
        ExportFormat,
        TrackerExporter,
    )

    choice = fmt.lower()
    formats: tuple[ExportFormat, ...]
    if choice == "both":
        formats = FORMATS
    elif choice == "xlsx":
        formats = ("xlsx",)
    elif choice == "csv":
        formats = ("csv",)
    else:
        console.print("[red]--format must be xlsx, csv or both.[/red]")
        raise typer.Exit(code=1)

    settings = load_settings()
    engine = create_db_engine(settings.database_file)
    initialize_database(engine)
    try:
        with session_scope(create_session_factory(engine)) as session:
            result = TrackerExporter(session, settings).export(formats)
    except (ExportError, OSError) as exc:
        console.print(f"[red]Export failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    finally:
        engine.dispose()
    console.print(f"rows={result.rows}")
    for path in result.files.values():
        console.print(f"  {path}", markup=False, highlight=False)


@cli.command()
def approve(
    application_id: Annotated[str, typer.Argument(help="For example APP-20260929-0001.")],
    api_url: str | None = typer.Option(None, help="Override JOB_AUTOMATION_API_URL."),
) -> None:
    """Review and approve one prepared application. You type the approval text yourself."""
    import httpx

    from scripts.approve_application import approve as run_approval

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print("[red]Approval must be typed by a person in an interactive terminal.[/red]")
        raise typer.Exit(code=1)
    settings = load_settings()
    headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
    try:
        with httpx.Client(
            base_url=api_url or settings.job_automation_api_url,
            headers=headers,
            timeout=200,
            trust_env=False,
        ) as client:
            code = run_approval(
                client,
                application_id,
                lambda prompt: typer.prompt(prompt, default="", show_default=False),
                lambda message: console.print(message, markup=False, highlight=False),
            )
    except httpx.HTTPError as exc:
        console.print(f"[red]Cannot reach the API:[/red] {type(exc).__name__}")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=code)


@cli.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


if __name__ == "__main__":
    cli()
