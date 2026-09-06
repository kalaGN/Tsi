"""验证 Pytest 收集阶段已经把模型日志与真实运行日志隔离。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app import application
from app.observability import model_logging


def _file_handler() -> RotatingFileHandler:
    """返回测试进程唯一的模型文件 Handler。"""

    handlers = [
        handler
        for handler in logging.getLogger(model_logging.LOGGER_NAME).handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(handlers) == 1
    return handlers[0]


def test_pytest_preconfigures_test_log_before_application_import():
    assert Path(_file_handler().baseFilename) == model_logging.TEST_LOG_PATH

    application.create_app()

    assert Path(_file_handler().baseFilename) == model_logging.TEST_LOG_PATH
    assert model_logging.TEST_LOG_PATH != model_logging.RUNTIME_LOG_PATH
