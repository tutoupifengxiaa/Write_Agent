from __future__ import annotations

from copy import deepcopy
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_session
from app.db.models import TrendSnapshot
from app.db.repositories import TrendSnapshotRepository
from app.services.trend_insights import build_trend_insights
from app.services.trend_providers import fetch_hotlists
from app.ui.radar_page import render_radar_page


router = APIRouter()


class DirectionGenerateRequest(BaseModel):
    direction_name: str
    selected_platforms: list[str]
    keywords: list[str]
    notes: str = ""
    capture_id: str | None = None
    selected_item_ids: list[str] = Field(default_factory=list)


PROVIDER_LABELS = {
    "fanqie": "番茄小说",
    "qimao": "七猫小说",
    "qidian": "起点中文网",
}
PLATFORM_PROVIDER_CODES = {
    "番茄": "fanqie",
    "番茄小说": "fanqie",
    "fanqie": "fanqie",
    "七猫": "qimao",
    "七猫小说": "qimao",
    "qimao": "qimao",
    "起点": "qidian",
    "起点中文网": "qidian",
    "qidian": "qidian",
}


@router.get("/radar", response_class=HTMLResponse)
def radar_page() -> HTMLResponse:
    return HTMLResponse(render_radar_page())


@router.get("/trends/hotlists")
def get_hotlists() -> dict[str, Any]:
    payload = dict(fetch_hotlists())
    payload["source_trace"] = None
    return payload


@router.get("/trends/insights")
def get_trend_insights(
    capture_id: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if capture_id is None:
        insights = build_trend_insights(fetch_hotlists())
        insights["source_trace"] = None
        return insights

    snapshots = TrendSnapshotRepository(session).list_snapshots(capture_id=capture_id)
    if not snapshots:
        raise HTTPException(status_code=404, detail="trend capture not found")
    insights = build_trend_insights(_snapshots_as_hotlists(snapshots))
    insights["source_trace"] = _build_source_trace(snapshots, selected_item_ids=[])
    return insights


@router.post("/trends/captures", status_code=201)
def create_trend_capture(
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    hotlists = fetch_hotlists()
    capture_id = f"capture_{uuid4().hex}"
    repo = TrendSnapshotRepository(session)
    snapshots: list[TrendSnapshot] = []
    for provider_payload in hotlists.get("providers", []):
        raw_items = provider_payload.get("items")
        raw_error = provider_payload.get("error")
        snapshots.append(
            repo.create_snapshot(
                capture_id=capture_id,
                provider=str(provider_payload.get("provider") or "unknown"),
                source_url=str(provider_payload.get("source_url") or ""),
                status=str(provider_payload.get("status") or "failed"),
                items=raw_items if isinstance(raw_items, list) else [],
                error_message=None if raw_error is None else str(raw_error),
            )
        )

    insights = build_trend_insights(_snapshots_as_hotlists(snapshots))
    insights["source_trace"] = _build_source_trace(snapshots, selected_item_ids=[])
    session.commit()
    return {
        "capture_id": capture_id,
        "snapshots": [_serialize_snapshot(snapshot) for snapshot in snapshots],
        "insights": insights,
    }


@router.get("/trends/snapshots")
def list_trend_snapshots(
    provider: str | None = None,
    capture_id: str | None = None,
    session: Session = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = TrendSnapshotRepository(session).list_snapshots(
        provider=provider,
        capture_id=capture_id,
    )
    return [_serialize_snapshot(row) for row in rows]


@router.get("/trends/snapshots/{snapshot_id}")
def get_trend_snapshot(
    snapshot_id: str,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = TrendSnapshotRepository(session).get_snapshot(snapshot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="trend snapshot not found")
    return _serialize_snapshot(row)


@router.post("/trends/generate-direction")
def generate_direction(
    payload: DirectionGenerateRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    generator = getattr(request.app.state, "direction_generator", None)
    if generator is None:
        raise HTTPException(status_code=503, detail="direction generator is not configured")

    generator_payload = payload.model_dump(exclude={"capture_id", "selected_item_ids"})
    source_trace = None
    if payload.capture_id is None:
        if payload.selected_item_ids:
            raise HTTPException(
                status_code=400,
                detail="selected_item_ids requires capture_id",
            )
    else:
        snapshots = TrendSnapshotRepository(session).list_snapshots(
            capture_id=payload.capture_id,
        )
        if not snapshots:
            raise HTTPException(status_code=404, detail="trend capture not found")
        _validate_selected_items(
            snapshots=snapshots,
            selected_platforms=payload.selected_platforms,
            selected_item_ids=payload.selected_item_ids,
        )
        source_trace = _build_source_trace(
            snapshots,
            selected_item_ids=payload.selected_item_ids,
        )
        generator_payload["source_trace"] = source_trace

    generated = generator(deepcopy(generator_payload))
    if not isinstance(generated, dict):
        raise HTTPException(
            status_code=502,
            detail="direction generator returned invalid result",
        )
    result = dict(generated)
    result["source_trace"] = source_trace
    return result


def _serialize_snapshot(snapshot: TrendSnapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "capture_id": snapshot.capture_id,
        "provider": snapshot.provider,
        "source_url": snapshot.source_url,
        "status": snapshot.status,
        "items": snapshot.items,
        "error_message": snapshot.error_message,
        "captured_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
    }


def _snapshots_as_hotlists(snapshots: list[TrendSnapshot]) -> dict[str, Any]:
    return {
        "providers": [
            {
                "provider": snapshot.provider,
                "provider_label": PROVIDER_LABELS.get(snapshot.provider, snapshot.provider),
                "source_url": snapshot.source_url,
                "status": snapshot.status,
                "error": snapshot.error_message,
                "items": snapshot.items,
            }
            for snapshot in snapshots
        ]
    }


def _build_source_trace(
    snapshots: list[TrendSnapshot],
    *,
    selected_item_ids: list[str],
) -> dict[str, Any]:
    captured_at = max(
        (snapshot.captured_at for snapshot in snapshots),
        default=None,
    )
    return {
        "capture_id": snapshots[0].capture_id,
        "snapshot_ids": [snapshot.id for snapshot in snapshots],
        "providers": [snapshot.provider for snapshot in snapshots],
        "selected_item_ids": selected_item_ids,
        "captured_at": captured_at.isoformat() if captured_at else None,
    }


def _validate_selected_items(
    *,
    snapshots: list[TrendSnapshot],
    selected_platforms: list[str],
    selected_item_ids: list[str],
) -> None:
    selected_providers = {
        PLATFORM_PROVIDER_CODES.get(platform.strip().lower(), platform.strip().lower())
        for platform in selected_platforms
    }
    item_providers: dict[str, str] = {}
    for snapshot in snapshots:
        for item in snapshot.items:
            item_id = item.get("id")
            if isinstance(item_id, str):
                item_providers[item_id] = snapshot.provider

    for item_id in selected_item_ids:
        provider = item_providers.get(item_id)
        if provider is None:
            raise HTTPException(
                status_code=400,
                detail=f"selected trend item not found in capture: {item_id}",
            )
        if provider not in selected_providers:
            raise HTTPException(
                status_code=400,
                detail=f"selected trend item does not belong to selected platforms: {item_id}",
            )
