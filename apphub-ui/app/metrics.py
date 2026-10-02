from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from app import mongodb_atlas as atlas

router = APIRouter()


def _is_up() -> bool:
    try:
        if atlas.USE_MONGODB:
            status = atlas.mongo_status()
            return bool(status.get("connected"))
        return True
    except Exception:
        return False


@router.get("/metrics")
def metrics() -> PlainTextResponse:
    up = _is_up()
    return PlainTextResponse(
        f"apphub_up {1 if up else 0}\n",
        status_code=200 if up else 503,
        media_type="text/plain; version=0.0.4",
    )
