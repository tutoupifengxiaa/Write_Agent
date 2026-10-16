from app.application import WriteAgentApplication
from app.config import settings

application = WriteAgentApplication.from_settings(settings)
app = application.fastapi_app
