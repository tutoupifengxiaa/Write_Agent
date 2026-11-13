from types import SimpleNamespace

from app.application import build_web_app
from app.config import settings

app = build_web_app(settings)
application = SimpleNamespace(fastapi_app=app)
