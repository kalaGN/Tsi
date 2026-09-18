"""Tsi 助手浏览器界面的公开组装入口。"""

from app.webui.router import create_webui_router
from app.webui.service import WebUiService

__all__ = ["WebUiService", "create_webui_router"]
