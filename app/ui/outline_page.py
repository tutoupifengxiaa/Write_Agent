from __future__ import annotations

from app.ui.page_shell import render_page_shell


def render_outline_page() -> str:
    return render_page_shell("大纲生成", "outline")
