from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.ui.library_page import render_library_page


router = APIRouter()


@router.get("/library", response_class=HTMLResponse)
def library_page() -> HTMLResponse:
    return HTMLResponse(render_library_page())
