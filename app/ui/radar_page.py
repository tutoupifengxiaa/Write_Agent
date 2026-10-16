from __future__ import annotations

from app.ui.page_shell import render_page_shell


def render_radar_page() -> str:
    return render_page_shell("选题雷达", "radar")
