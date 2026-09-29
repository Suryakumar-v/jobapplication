from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.dependencies import require_api_key
from app.main import create_app


def _app_with_protected_route(tmp_path: Path, api_key: str) -> FastAPI:
    settings = Settings(
        project_root=tmp_path, config_directory=PROJECT_ROOT / "config", api_key=api_key
    )
    app = create_app(settings)
    router = APIRouter()

    @router.get("/protected", dependencies=[Depends(require_api_key)])
    def protected() -> dict[str, bool]:
        return {"ok": True}

    app.include_router(router)
    return app


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 401), ({"X-API-Key": "wrong"}, 401), ({"X-API-Key": "correct-key"}, 200)],
)
def test_api_key_enforced(tmp_path: Path, headers: dict[str, str], status: int) -> None:
    with TestClient(_app_with_protected_route(tmp_path, "correct-key")) as client:
        assert client.get("/protected", headers=headers).status_code == status


def test_no_api_key_configured_allows_local_access(tmp_path: Path) -> None:
    with TestClient(_app_with_protected_route(tmp_path, "")) as client:
        assert client.get("/protected").status_code == 200
