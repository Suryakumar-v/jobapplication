"""Minimal schema versioning: create missing tables and record the schema version."""

from __future__ import annotations

from sqlalchemy import Engine, func, inspect, select, text
from sqlalchemy.orm import Session

from app.database import tables
from app.database.base import Base

SCHEMA_VERSION = 4

# v2: matching results stored on the job record.
_V2_JOB_COLUMNS = ("matching_details", "analyzed_at")
# v3: form_runs table, created by create_all; no column changes.
# v4: ai_analyses table, created by create_all; no column changes.


class SchemaVersionError(RuntimeError):
    pass


def get_schema_version(engine: Engine) -> int | None:
    if not inspect(engine).has_table(tables.SchemaVersionRecord.__tablename__):
        return None
    with Session(engine) as session:
        return session.scalar(select(func.max(tables.SchemaVersionRecord.version)))


def _upgrade_jobs_to_v2(engine: Engine) -> None:
    existing = {column["name"] for column in inspect(engine).get_columns("jobs")}
    jobs = tables.JobRecord.__table__
    with engine.begin() as connection:
        for name in _V2_JOB_COLUMNS:
            if name not in existing:
                column_type = jobs.c[name].type.compile(dialect=engine.dialect)
                connection.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}"))
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_jobs_analyzed_at ON jobs (analyzed_at)")
        )


def initialize_database(engine: Engine) -> int:
    """Create tables if needed. Refuses to run against a newer, unknown schema."""
    current = get_schema_version(engine)
    if current is not None and current > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Database schema v{current} is newer than supported v{SCHEMA_VERSION}."
        )
    Base.metadata.create_all(engine)
    if current is not None and current < 2:
        _upgrade_jobs_to_v2(engine)
    if current is None or current < SCHEMA_VERSION:
        with Session(engine) as session:
            session.add(tables.SchemaVersionRecord(version=SCHEMA_VERSION))
            session.commit()
    return SCHEMA_VERSION
