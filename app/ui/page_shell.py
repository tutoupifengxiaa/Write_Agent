from __future__ import annotations

from html import escape


def render_page_shell(title: str, page: str, *, extra_scripts: tuple[str, ...] = ()) -> str:
    title = escape(title)
    page = escape(page, quote=True)
    scripts = "\n".join(f'  <script src="/static/{escape(script, quote=True)}"></script>' for script in extra_scripts)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} | Write Agent</title>
  <link rel="stylesheet" href="/static/common.css">
  <link rel="stylesheet" href="/static/{page}.css">
</head>
<body>
  <div id="{page}-root"></div>
  <div id="app-loading-fallback" role="status" style="padding:24px;color:#583e2d;background:#fff7ed;font-family:sans-serif">
    <strong>页面加载中…</strong>
    <p>若长时间未显示内容，可能是页面资源加载失败。请检查网络后 <a href="">重新加载</a>。</p>
    <noscript>请启用 JavaScript 后使用此页面。</noscript>
  </div>
  <script>
    (function () {{
      var root = document.getElementById("{page}-root");
      var fallback = document.getElementById("app-loading-fallback");
      var observer = new MutationObserver(function () {{
        if (root.childNodes.length) {{ fallback.remove(); observer.disconnect(); }}
      }});
      observer.observe(root, {{ childList: true }});
      window.addEventListener("error", function (event) {{
        if (fallback.isConnected && (!event.target || event.target.tagName === "SCRIPT" || event.error)) {{
          fallback.querySelector("strong").textContent = "页面加载失败，请检查网络后重试。";
        }}
      }}, true);
    }})();
  </script>
  <script crossorigin src="https://unpkg.com/react@18.3.1/umd/react.production.min.js"></script>
  <script crossorigin src="https://unpkg.com/react-dom@18.3.1/umd/react-dom.production.min.js"></script>
  <script src="/static/common.js"></script>
{scripts}
  <script src="/static/{page}-app.js"></script>
</body>
</html>
"""
