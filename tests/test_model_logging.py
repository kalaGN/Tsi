import io
import json
import logging
from logging.handlers import RotatingFileHandler

import pytest

from app.observability import model_logging


@pytest.fixture(autouse=True)
def reset_model_logger():
    """每个测试独立管理 Handler，避免全局 logging 状态相互污染。"""

    logger = logging.getLogger(model_logging.LOGGER_NAME)
    original_handlers = list(logger.handlers)
    original_level = logger.level
    original_propagate = logger.propagate
    for handler in original_handlers:
        logger.removeHandler(handler)

    yield

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    for handler in original_handlers:
        logger.addHandler(handler)
    logger.setLevel(original_level)
    logger.propagate = original_propagate


def test_model_input_and_output_are_single_line_json_in_stream_and_file(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "nested" / "model-calls.log"
    input_text = '你好\n请回答 "JSON"'
    output_text = "可以\\done\n第二行"

    model_logging.configure_model_logging(stream=stream, log_path=log_path)
    model_logging.log_model_request(
        request_id="0123456789abcdef0123456789abcdef",
        provider="deepseek",
        model="deepseek-v4-flash",
        input_chars=len(input_text),
        input_text=input_text,
    )
    model_logging.log_model_response(
        request_id="0123456789abcdef0123456789abcdef",
        provider="deepseek",
        model="deepseek-v4-flash",
        output_chars=len(output_text),
        output_text=output_text,
    )

    stream_lines = stream.getvalue().splitlines()
    file_lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(stream_lines) == 2
    assert file_lines == stream_lines

    request_event = json.loads(stream_lines[0])
    assert set(request_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "provider",
        "model",
        "input_chars",
        "input_text",
    }
    assert request_event["timestamp"].endswith("Z")
    assert request_event["level"] == "INFO"
    assert request_event["event"] == "llm_request"
    assert request_event["request_id"] == "0123456789abcdef0123456789abcdef"
    assert request_event["provider"] == "deepseek"
    assert request_event["model"] == "deepseek-v4-flash"
    assert request_event["input_chars"] == len(input_text)
    assert request_event["input_text"] == input_text

    response_event = json.loads(stream_lines[1])
    assert set(response_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "provider",
        "model",
        "output_chars",
        "output_text",
    }
    assert response_event["event"] == "llm_response"
    assert response_event["request_id"] == request_event["request_id"]
    assert response_event["output_chars"] == len(output_text)
    assert response_event["output_text"] == output_text


def test_configuration_is_idempotent_for_both_handlers(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "model-calls.log"

    model_logging.configure_model_logging(stream=stream, log_path=log_path)
    model_logging.configure_model_logging(stream=stream, log_path=log_path)
    model_logging.log_model_request(
        request_id="a" * 32,
        provider="aliyun",
        model="qwen3-max",
        input_chars=2,
        input_text="你好",
    )

    logger = logging.getLogger(model_logging.LOGGER_NAME)
    assert len(logger.handlers) == 2
    assert logger.propagate is False
    assert len(stream.getvalue().splitlines()) == 1
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 1


def test_rotating_file_keeps_at_most_configured_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(model_logging, "MAX_LOG_BYTES", 100)
    log_path = tmp_path / "model-calls.log"
    model_logging.configure_model_logging(stream=io.StringIO(), log_path=log_path)

    for index in range(10):
        model_logging.log_model_request(
            request_id=f"{index:032x}",
            provider="deepseek",
            model="deepseek-v4-flash",
            input_chars=index,
            input_text="x" * index,
        )

    backups = list(tmp_path.glob("model-calls.log.*"))
    assert len(backups) == model_logging.BACKUP_COUNT
    file_handler = next(
        handler
        for handler in logging.getLogger(model_logging.LOGGER_NAME).handlers
        if isinstance(handler, RotatingFileHandler)
    )
    assert file_handler.maxBytes == 100
    assert file_handler.backupCount == 5


def test_unavailable_file_falls_back_to_stream(tmp_path):
    stream = io.StringIO()
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("block", encoding="utf-8")

    model_logging.configure_model_logging(
        stream=stream,
        log_path=blocking_file / "model-calls.log",
    )
    model_logging.log_model_request(
        request_id="b" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        input_chars=1,
        input_text="x",
    )

    assert json.loads(stream.getvalue())["event"] == "llm_request"
    logger = logging.getLogger(model_logging.LOGGER_NAME)
    assert len(logger.handlers) == 1


def test_request_id_is_uuid_hex():
    request_id = model_logging.new_request_id()

    assert len(request_id) == 32
    assert int(request_id, 16) >= 0
