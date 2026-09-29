"""Tracker export endpoint. Writes files under EXPORT_DIRECTORY; never returns row data."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.dependencies import ApiKeyDep, SessionDep, SettingsDep
from app.services.export_service import (
    FORMATS,
    ExportError,
    ExportFileLockedError,
    ExportFormat,
    TrackerExporter,
)

router = APIRouter(prefix="/exports", tags=["exports"], dependencies=[ApiKeyDep])


class ExportRequest(BaseModel):
    formats: Annotated[list[ExportFormat], Field(min_length=1, max_length=2)] = list(FORMATS)


class ExportResponse(BaseModel):
    rows: int
    generated_at: datetime
    files: dict[ExportFormat, str]


@router.post("/tracker", response_model=ExportResponse)
def export_tracker(
    session: SessionDep, settings: SettingsDep, body: ExportRequest | None = None
) -> ExportResponse:
    formats = tuple((body or ExportRequest()).formats)
    try:
        result = TrackerExporter(session, settings).export(formats)
    except ExportFileLockedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (ExportError, OSError) as exc:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Export failed") from exc
    return ExportResponse(
        rows=result.rows,
        generated_at=result.generated_at,
        files={fmt: path.name for fmt, path in result.files.items()},
    )
