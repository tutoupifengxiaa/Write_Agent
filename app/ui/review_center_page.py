from __future__ import annotations

from app.ui.page_shell import render_page_shell


def render_review_center_page() -> str:
    return render_page_shell("人工审核台", "review-center")
