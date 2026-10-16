from app.services.fanqie_browser import DraftUploadResult


def ensure_publishable(status: str) -> None:
    if status not in {"persisted", "human_review_optional", "publish_pending"}:
        raise ValueError(f"Chapter status {status!r} is not publishable")


def publish_draft(
    *,
    browser,
    chapter_title: str,
    chapter_content: str,
    publish_mode: str,
) -> DraftUploadResult:
    if publish_mode != "draft_only":
        raise ValueError("Only draft_only mode is supported")

    return browser.upload_draft(chapter_title, chapter_content)
