"""Create the SQLite database and required directories (idempotent)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console

from app.config import Settings, load_settings
from app.database.migrations import initialize_database
from app.database.session import create_db_engine
from app.utils.file_utils import ensure_directories


def initialize(settings: Settings) -> int:
    ensure_directories(settings.required_directories().values())
    engine = create_db_engine(settings.database_file)
    try:
        return initialize_database(engine)
    finally:
        engine.dispose()


def main() -> int:
    settings = load_settings()
    version = initialize(settings)
    Console().print(f"Database ready at {settings.database_file} (schema v{version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
