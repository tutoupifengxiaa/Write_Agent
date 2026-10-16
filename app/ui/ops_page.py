from __future__ import annotations

from app.ui.page_shell import render_page_shell


def render_ops_page() -> str:
    return render_page_shell("运营看板", "ops")
