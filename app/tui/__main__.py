import os
from pathlib import Path

from dotenv import load_dotenv


def _create_app():
    from app.tui.application import ChatTuiApp

    return ChatTuiApp()


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env", override=False)
    os.environ["TEXTUAL_DISABLE_KITTY_KEY"] = "1"
    _create_app().run()


if __name__ == "__main__":
    main()
