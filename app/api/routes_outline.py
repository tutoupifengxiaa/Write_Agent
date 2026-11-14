from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.ui.outline_page import render_outline_page

router = APIRouter()


class OutlineGenerateRequest(BaseModel):
    title: str
    platform: str
    genre: str
    direction_name: str = ""
    core_hook: str = ""
    audience: str = ""
    notes: str = ""


@router.get("/outline", response_class=HTMLResponse)
def outline_page() -> HTMLResponse:
    return HTMLResponse(render_outline_page())


@router.post("/outline/generate")
def generate_outline(payload: OutlineGenerateRequest, request: Request) -> dict[str, Any]:
    generator = getattr(request.app.state, "outline_generator", None)
    if generator is None:
        raise HTTPException(status_code=503, detail="outline generator is not configured")
    return generator(payload.model_dump())
