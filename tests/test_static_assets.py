import re
from hashlib import sha256

from app.ui import page_shell
from app.ui.creation_page import render_creation_page


def test_shell_versions_styles_and_scripts_by_content():
    html = render_creation_page()
    urls = re.findall(r'(?:src|href)="(/static/[^"]+)"', html)
    expected = [
        "common.css", "creation.css", "vendor/react.production.min.js",
        "vendor/react-dom.production.min.js", "common.js", "creation-stream.js",
        "creation-panels.js", "creation-app.js",
    ]
    for url, asset in zip(urls, expected, strict=True):
        digest = sha256((page_shell.STATIC_DIR / asset).read_bytes()).hexdigest()[:12]
        assert url == f"/static/{asset}?v={digest}"


def test_static_version_snapshot_is_reused_until_restart(tmp_path, monkeypatch):
    path = tmp_path / "example.js"
    path.write_bytes(b"first")
    monkeypatch.setattr(page_shell, "STATIC_VERSIONS", page_shell._static_versions(tmp_path))
    first_url = page_shell.static_url(path.name)
    path.write_bytes(b"second")
    assert page_shell.static_url(path.name) == first_url
    assert page_shell.static_url("absent.js") == "/static/absent.js"

    monkeypatch.setattr(page_shell, "STATIC_VERSIONS", page_shell._static_versions(tmp_path))
    digest = sha256(b"second").hexdigest()[:12]
    assert page_shell.static_url(path.name) == f"/static/example.js?v={digest}"
    assert page_shell.static_url(path.name) != first_url
