from __future__ import annotations


def render_hub_page() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Write Agent Hub</title>
  <style>
    body { font-family: "Segoe UI", sans-serif; margin: 0; background: #f5efe4; color: #1f1a14; }
    main { max-width: 980px; margin: 0 auto; padding: 48px 24px; }
    h1 { font-size: 48px; margin: 0 0 12px; }
    p { color: #665f57; line-height: 1.8; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-top: 28px; }
    a { display: block; padding: 18px; border-radius: 18px; text-decoration: none; color: inherit; background: #fffaf2; border: 1px solid rgba(31, 26, 20, 0.12); }
    strong { display: block; font-size: 20px; margin-bottom: 8px; }
  </style>
</head>
<body>
  <main>
    <h1>Write Agent</h1>
    <p>统一入口：在这里切换运营、选题、文章管理、大纲生成、人工审核与单章创作页面。</p>
    <div class="grid">
      <a href="/ops"><strong>运营看板</strong><span>查看调度、失败聚类与发布任务。</span></a>
      <a href="/library"><strong>文章管理</strong><span>查看书籍、章节与发布状态。</span></a>
      <a href="/outline"><strong>大纲生成</strong><span>生成书籍大纲并回写书籍记录。</span></a>
      <a href="/radar"><strong>选题雷达</strong><span>获取热门榜单与方向包。</span></a>
      <a href="/review-center"><strong>人工审核台</strong><span>集中处理待审核与待修订章节。</span></a>
      <a href="/creation"><strong>创作台</strong><span>进行单章生成与流式写作。</span></a>
    </div>
  </main>
</body>
</html>
"""
