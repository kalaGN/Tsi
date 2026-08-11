"""`python -m app.tui` 的终端启动入口。"""

import os
from pathlib import Path

from dotenv import load_dotenv

from app.observability.model_logging import configure_model_logging
from app.runtime.system_prompt import SystemPromptLoadError, load_system_prompt


def _create_app(
    *,
    system_prompt: str | None,
    system_prompt_error: str | None,
):
    """延迟导入 Textual，确保终端兼容配置先于框架初始化生效。"""

    from app.tui.application import ChatTuiApp

    return ChatTuiApp(
        system_prompt=system_prompt,
        system_prompt_error=system_prompt_error,
    )


def main() -> None:
    """加载项目环境并启动本地 TUI。"""

    startup_directory = Path.cwd()
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    # Report-all-keys 会干扰部分 macOS 中文输入法，因此项目不启用该协议。
    os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"
    configure_model_logging()
    try:
        system_prompt = load_system_prompt(startup_directory)
        system_prompt_error = None
    except SystemPromptLoadError as exc:
        system_prompt = None
        system_prompt_error = str(exc)
    _create_app(
        system_prompt=system_prompt,
        system_prompt_error=system_prompt_error,
    ).run()


if __name__ == "__main__":
    main()
