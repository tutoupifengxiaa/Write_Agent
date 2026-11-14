"""Run tests without loading local credentials or the application database."""
import os
from pathlib import Path
import socket
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pydantic_settings.sources import DotEnvSettingsSource

DotEnvSettingsSource.__call__ = lambda self: {}
os.environ.update(DATABASE_URL="sqlite+pysqlite:///:memory:", OPENAI_API_KEY="", EMBEDDING_API_KEY="", FANQIE_BROWSER_MODE="stub", PUBLISH_MODE="")
_connect = socket.socket.connect


def local_only(self, address):
    if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError("External network disabled during tests")
    return _connect(self, address)


socket.socket.connect = local_only
import pytest

raise SystemExit(pytest.main(sys.argv[1:] or ["-q", "--tb=short"]))
