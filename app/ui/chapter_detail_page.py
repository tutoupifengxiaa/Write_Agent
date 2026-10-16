from __future__ import annotations


def render_chapter_detail_page(detail: dict) -> str:
    chapter = detail["chapter"]
    latest_job = detail.get("latest_job")
    latest_version = detail.get("latest_version")
    versions = detail.get("versions") or []
    publish_jobs = detail.get("publish_jobs") or []

    latest_job_html = "<p>暂无发布任务</p>"
    if latest_job is not None:
        latest_job_html = f"""
        <div class="detail-card">
          <span>发布任务</span>
          <strong>{latest_job["status"]}</strong>
          <p>job_id: {latest_job["id"]}</p>
          <p>platform: {latest_job["platform"]}</p>
          <p>error: {latest_job["error_message"] or "-"}</p>
        </div>
        """

    latest_version_html = "<p>暂无版本内容</p>"
    if latest_version is not None:
        latest_version_html = f"""
        <div class="detail-card detail-card-wide">
          <span>最近版本摘要</span>
          <strong>{latest_version["summary"] or "-"}</strong>
          <p>review_score: {latest_version["review_score"]}</p>
          <pre>{latest_version["content"] or "-"}</pre>
        </div>
        """

    version_history_html = "<p>暂无版本历史</p>"
    if versions:
        version_history_html = "".join(
            f"""
            <div class="detail-card">
              <span>版本 {index + 1}</span>
              <strong>{version["summary"] or "-"}</strong>
              <p>review_score: {version["review_score"]}</p>
              <p>{version["created_at"] or "-"}</p>
            </div>
            """
            for index, version in enumerate(versions)
        )

    publish_history_html = "<p>暂无发布历史</p>"
    if publish_jobs:
        publish_history_html = "".join(
            f"""
            <div class="detail-card">
              <span>发布记录</span>
              <strong>{job["status"]}</strong>
              <p>job_id: {job["id"]}</p>
              <p>platform: {job["platform"]}</p>
              <p>mode: {job["mode"]}</p>
              <p>error: {job["error_message"] or "-"}</p>
            </div>
            """
            for job in publish_jobs
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{chapter["title"]} | 章节详情</title>
  <style>
    body {{
      margin: 0;
      font-family: "Iowan Old Style", "Palatino Linotype", "Songti SC", serif;
      background: linear-gradient(135deg, #f9efe5 0%, #efe2d3 48%, #d8b999 100%);
      color: #16120e;
    }}
    main {{
      width: min(1180px, calc(100% - 40px));
      margin: 0 auto;
      padding: 30px 0 44px;
    }}
    .hero, .panel {{
      border: 1px solid rgba(54, 42, 29, 0.14);
      border-radius: 28px;
      background: rgba(255, 249, 241, 0.92);
      box-shadow: 0 24px 72px rgba(54, 39, 22, 0.14);
    }}
    .hero {{
      padding: 28px;
    }}
    .hero h1 {{
      margin: 0;
      font-size: clamp(36px, 5vw, 64px);
      line-height: 0.98;
    }}
    .hero p {{
      color: #6f665c;
      line-height: 1.8;
    }}
    .nav {{
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-top: 18px;
    }}
    .nav a {{
      padding: 10px 14px;
      border-radius: 999px;
      border: 1px solid rgba(54, 42, 29, 0.14);
      text-decoration: none;
      color: inherit;
      background: rgba(255, 255, 255, 0.46);
    }}
    .layout {{
      display: grid;
      grid-template-columns: 1.2fr 0.9fr;
      gap: 18px;
      margin-top: 20px;
    }}
    .panel {{
      padding: 24px;
    }}
    .panel h2 {{
      margin: 0 0 14px;
      font-size: 24px;
    }}
    .panel h3 {{
      margin: 22px 0 14px;
      font-size: 20px;
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
    }}
    .detail-card {{
      padding: 16px;
      border: 1px solid rgba(54, 42, 29, 0.14);
      border-radius: 20px;
      background: rgba(255, 253, 248, 0.96);
    }}
    .detail-card-wide {{
      grid-column: 1 / -1;
    }}
    .detail-card span {{
      display: block;
      color: #6f665c;
      font-size: 12px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
    }}
    .detail-card strong {{
      display: block;
      margin-top: 8px;
      font-size: 20px;
    }}
    pre {{
      white-space: pre-wrap;
      line-height: 1.8;
      font: inherit;
    }}
    @media (max-width: 980px) {{
      .layout, .grid {{
        grid-template-columns: 1fr;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="hero">
      <h1>{chapter["title"]}</h1>
      <p>{chapter["book_title"]} | 第 {chapter["chapter_number"]} 章 | 当前状态：{chapter["status"]}</p>
      <div class="nav">
        <a href="/library">文章管理</a>
        <a href="/creation?book_id={chapter["book_id"]}&chapter_id={chapter["id"]}">回到创作页</a>
        <a href="/review-center">人工审核台</a>
        <a href="/ops">运营看板</a>
      </div>
    </section>
    <section class="layout">
      <section class="panel">
        <h2>章节信息</h2>
        <div class="grid">
          <div class="detail-card">
            <span>章节目标</span>
            <strong>{chapter["goal"].get("objective", "-")}</strong>
          </div>
          <div class="detail-card">
            <span>人工审核备注</span>
            <strong>{chapter["goal"].get("manual_review_note", "-")}</strong>
          </div>
          {latest_version_html}
        </div>
        <h3>版本历史</h3>
        <div class="grid">
          {version_history_html}
        </div>
      </section>
      <aside class="panel">
        <h2>发布信息</h2>
        {latest_job_html}
        <h3>发布历史</h3>
        <div class="grid">
          {publish_history_html}
        </div>
      </aside>
    </section>
  </main>
</body>
</html>
"""
