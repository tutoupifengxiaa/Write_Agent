from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.ui.hub_page import render_hub_page

router = APIRouter()


@router.get("/hub", response_class=HTMLResponse)
def hub_page() -> HTMLResponse:
    return HTMLResponse(render_hub_page())
