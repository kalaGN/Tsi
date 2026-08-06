"""`python -m app.tui` 的终端启动入口。"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _create_app():
    """延迟导入 Textual，确保终端兼容配置先于框架初始化生效。"""

    from app.tui.application import ChatTuiApp

    return ChatTuiApp()


def main() -> None:
    """加载项目环境并启动本地 TUI。"""

    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    # Report-all-keys 会干扰部分 macOS 中文输入法，因此项目不启用该协议。
    os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"
    _create_app().run()


if __name__ == "__main__":
    main()
