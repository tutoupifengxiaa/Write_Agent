from __future__ import annotations

from app.ui.page_shell import render_page_shell


def render_creation_page() -> str:
    return render_page_shell("创作驾驶舱", "creation", extra_scripts=("creation-stream.js", "creation-panels.js"))
