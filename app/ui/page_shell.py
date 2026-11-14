from __future__ import annotations

from hashlib import sha256
from html import escape
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _static_versions(directory: Path) -> dict[str, str]:
    versions = {}
    for path in directory.rglob("*"):
        if path.is_file():
            try:
                versions[path.relative_to(directory).as_posix()] = sha256(path.read_bytes()).hexdigest()[:12]
            except FileNotFoundError:
                continue
    return versions


STATIC_VERSIONS = _static_versions(STATIC_DIR)


def static_url(filename: str) -> str:
    url = f"/static/{filename}"
    version = STATIC_VERSIONS.get(filename)
    return f"{url}?v={version}" if version else url


def render_page_shell(title: str, page: str, *, extra_scripts: tuple[str, ...] = ()) -> str:
    title = escape(title)
    stylesheet = escape(static_url(f"{page}.css"))
    page_script = escape(static_url(f"{page}-app.js"))
    page = escape(page, quote=True)
    scripts = "\n".join(f'  <script src="{escape(static_url(script))}"></script>' for script in extra_scripts)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} | Write Agent</title>
  <link rel="stylesheet" href="{static_url("common.css")}">
  <link rel="stylesheet" href="{stylesheet}">
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
  <script src="{static_url("vendor/react.production.min.js")}"></script>
  <script src="{static_url("vendor/react-dom.production.min.js")}"></script>
  <script src="{static_url("common.js")}"></script>
{scripts}
  <script src="{page_script}"></script>
</body>
</html>
"""
