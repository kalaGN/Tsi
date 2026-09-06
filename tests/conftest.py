"""Pytest 全局初始化：在收集应用模块前隔离测试模型日志。"""

from app.observability.model_logging import (
    TEST_LOG_PATH,
    configure_model_logging,
)


# conftest 先于测试模块导入，后续应用默认配置会复用此文件 Handler。
configure_model_logging(log_path=TEST_LOG_PATH)
