import io
import json
import logging
import re
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


def test_stream_stays_json_while_file_uses_readable_model_blocks(tmp_path):
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
    file_text = log_path.read_text(encoding="utf-8")
    assert len(stream_lines) == 2

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

    assert re.search(
        r"时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \+08:00",
        file_text,
    )
    assert "事件：模型请求" in file_text
    assert "事件：模型响应" in file_text
    assert "请求ID：0123456789abcdef0123456789abcdef" in file_text
    assert "Provider：deepseek" in file_text
    assert "模型：deepseek-v4-flash" in file_text
    assert f"输入长度：{len(input_text)} 字符" in file_text
    assert f"输出长度：{len(output_text)} 字符" in file_text
    assert f"【输入内容】\n{input_text}" in file_text
    assert f"【输出内容】\n{output_text}" in file_text
    assert file_text.count("=" * 80) == 2


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
    assert log_path.read_text(encoding="utf-8").count("=" * 80) == 1


def test_file_only_configuration_removes_stream_handler(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "model-calls.log"
    model_logging.configure_model_logging(stream=stream, log_path=log_path)

    # TUI 可能与其他入口运行在同一测试进程；切换后也必须移除已有终端输出。
    model_logging.configure_model_logging(
        stream=stream,
        log_path=log_path,
        enable_stream=False,
    )
    model_logging.log_model_request(
        request_id="c" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        input_chars=2,
        input_text="你好",
    )

    logger = logging.getLogger(model_logging.LOGGER_NAME)
    assert stream.getvalue() == ""
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0], RotatingFileHandler)
    assert "事件：模型请求" in log_path.read_text(encoding="utf-8")


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


def test_http_log_events_use_exact_whitelist_and_single_line_json(tmp_path):
    stream = io.StringIO()
    log_path = tmp_path / "model-calls.log"
    model_logging.configure_model_logging(stream=stream, log_path=log_path)

    request_body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": False,
    }
    timeout = {"connect_seconds": 10.0, "total_seconds": 60.0}

    model_logging.log_model_http_request(
        request_id="a" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        method="POST",
        url="https://api.deepseek.com/chat/completions",
        request_body=request_body,
        timeout=timeout,
    )
    model_logging.log_model_http_response(
        request_id="a" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        status_code=200,
        duration_ms=12.34,
        response_content_type="application/json",
    )
    model_logging.log_model_http_error(
        request_id="b" * 32,
        provider="aliyun",
        model="qwen3-max",
        error_type="timeout",
        duration_ms=9999.5,
    )

    stream_lines = stream.getvalue().splitlines()
    file_text = log_path.read_text(encoding="utf-8")
    assert len(stream_lines) == 3

    request_event = json.loads(stream_lines[0])
    assert set(request_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "provider",
        "model",
        "method",
        "url",
        "headers",
        "request_body",
        "timeout",
    }
    assert request_event["level"] == "INFO"
    assert request_event["event"] == "llm_http_request"
    assert request_event["method"] == "POST"
    assert request_event["url"] == "https://api.deepseek.com/chat/completions"
    assert request_event["headers"] == {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": "Bearer [REDACTED]",
    }
    assert request_event["request_body"] == request_body
    assert request_event["timeout"] == timeout

    response_event = json.loads(stream_lines[1])
    assert set(response_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "provider",
        "model",
        "status_code",
        "duration_ms",
        "response_content_type",
    }
    assert response_event["event"] == "llm_http_response"
    assert response_event["status_code"] == 200
    assert response_event["duration_ms"] == 12.34
    assert response_event["response_content_type"] == "application/json"
    assert response_event["request_id"] == request_event["request_id"]

    error_event = json.loads(stream_lines[2])
    assert set(error_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "provider",
        "model",
        "error_type",
        "duration_ms",
    }
    assert error_event["event"] == "llm_http_error"
    assert error_event["error_type"] == "timeout"
    assert error_event["duration_ms"] == 9999.5

    assert "事件：HTTP 请求" in file_text
    assert "方法：POST" in file_text
    assert "URL：https://api.deepseek.com/chat/completions" in file_text
    assert "【请求 Header】" in file_text
    assert '  "Authorization": "Bearer [REDACTED]"' in file_text
    assert "【超时配置】" in file_text
    assert '  "connect_seconds": 10.0' in file_text
    assert "【请求体】" in file_text
    assert '      "content": "你好"' in file_text
    assert "事件：HTTP 响应" in file_text
    assert "HTTP 状态：200" in file_text
    assert "响应类型：application/json" in file_text
    assert "事件：HTTP 错误" in file_text
    assert "错误类型：超时" in file_text
    assert file_text.count("=" * 80) == 3


def test_http_request_body_preserves_multi_turn_chinese_newlines_and_quotes(
    tmp_path,
):
    stream = io.StringIO()
    model_logging.configure_model_logging(
        stream=stream,
        log_path=tmp_path / "model-calls.log",
    )
    body = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "user", "content": '第一问\n带 "引号"'},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "第二问"},
        ],
        "stream": False,
    }

    model_logging.log_model_http_request(
        request_id="c" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        method="POST",
        url="https://api.deepseek.com/chat/completions",
        request_body=body,
        timeout={"connect_seconds": 10.0, "total_seconds": 60.0},
    )

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "\n" not in line
    event = json.loads(line)
    assert event["request_body"] == body
    assert event["request_body"]["messages"][0]["content"] == '第一问\n带 "引号"'


def test_http_log_events_never_leak_api_key_or_raw_details(tmp_path):
    stream = io.StringIO()
    model_logging.configure_model_logging(
        stream=stream,
        log_path=tmp_path / "model-calls.log",
    )
    secret_key = "sk-test-secret-key-must-not-leak"

    model_logging.log_model_http_request(
        request_id="d" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        method="POST",
        url="https://api.deepseek.com/chat/completions",
        request_body={"model": "deepseek-v4-flash", "messages": []},
        timeout={"connect_seconds": 10.0, "total_seconds": 60.0},
    )
    model_logging.log_model_http_response(
        request_id="d" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        status_code=500,
        duration_ms=1.0,
        response_content_type="application/json",
    )
    model_logging.log_model_http_error(
        request_id="e" * 32,
        provider="deepseek",
        model="deepseek-v4-flash",
        error_type="connection",
        duration_ms=2.0,
    )

    text = stream.getvalue()
    assert secret_key not in text
    assert "Bearer [REDACTED]" in text

    response_event = json.loads(text.splitlines()[1])
    assert "raw_body" not in response_event
    assert "body" not in response_event

    error_event = json.loads(text.splitlines()[2])
    serialized = json.dumps(error_event, ensure_ascii=False)
    assert "exception" not in serialized.lower()
    assert "traceback" not in serialized.lower()


def test_tool_events_record_arguments_and_results_with_exact_whitelist(tmp_path):
    stream = io.StringIO()
    model_logging.configure_model_logging(
        stream=stream,
        log_path=tmp_path / "model-calls.log",
    )

    model_logging.log_model_tool_call(
        request_id="f" * 32,
        call_id="call-time-1",
        tool_name="get_current_time",
        arguments_chars=28,
        arguments_json='{"timezone":"Asia/Shanghai"}',
    )
    model_logging.log_model_tool_result(
        request_id="f" * 32,
        call_id="call-time-1",
        tool_name="get_current_time",
        status="success",
        duration_ms=1.25,
        output_chars=91,
        output_text='{"ok":true,"data":{"timezone":"Asia/Shanghai"}}',
    )

    call_event, result_event = map(
        json.loads,
        stream.getvalue().splitlines(),
    )
    assert set(call_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "call_id",
        "tool_name",
        "arguments_chars",
        "arguments_json",
    }
    assert call_event["event"] == "llm_tool_call"
    assert call_event["call_id"] == "call-time-1"
    assert call_event["arguments_chars"] == 28
    assert call_event["arguments_json"] == '{"timezone":"Asia/Shanghai"}'

    assert set(result_event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "call_id",
        "tool_name",
        "status",
        "duration_ms",
        "output_chars",
        "output_text",
    }
    assert result_event["event"] == "llm_tool_result"
    assert result_event["status"] == "success"
    assert result_event["duration_ms"] == 1.25
    assert result_event["output_chars"] == 91
    assert json.loads(result_event["output_text"])["ok"] is True
    serialized = json.dumps([call_event, result_event], ensure_ascii=False)
    assert "Asia/Shanghai" in serialized
    assert "exception" not in serialized.lower()

    file_text = (tmp_path / "model-calls.log").read_text(encoding="utf-8")
    assert "事件：工具调用" in file_text
    assert "调用ID：call-time-1" in file_text
    assert "工具：get_current_time" in file_text
    assert "参数长度：28 字符" in file_text
    assert "【工具参数】\n{\n  \"timezone\": \"Asia/Shanghai\"\n}" in file_text
    assert "事件：工具结果" in file_text
    assert "状态：成功" in file_text
    assert "耗时：1.25 ms" in file_text
    assert "输出长度：91 字符" in file_text
    assert '    "timezone": "Asia/Shanghai"' in file_text


def test_tool_approval_event_uses_metadata_only_whitelist(tmp_path):
    stream = io.StringIO()
    model_logging.configure_model_logging(
        stream=stream,
        log_path=tmp_path / "model-calls.log",
    )

    model_logging.log_model_tool_approval(
        request_id="a" * 32,
        call_id="call-write-1",
        tool_name="apply_workspace_edits",
        approved=False,
        paths_count=2,
        diff_chars=123,
        duration_ms=8.5,
    )

    event = json.loads(stream.getvalue())
    assert set(event) == {
        "timestamp",
        "level",
        "event",
        "request_id",
        "call_id",
        "tool_name",
        "approved",
        "paths_count",
        "diff_chars",
        "duration_ms",
    }
    assert event["event"] == "llm_tool_approval"
    assert event["approved"] is False
    assert "diff_text" not in event
    assert "path" not in event

    file_text = (tmp_path / "model-calls.log").read_text(encoding="utf-8")
    assert "事件：工具审批" in file_text
    assert "审批结果：拒绝" in file_text
    assert "路径数量：2" in file_text
    assert "Diff 长度：123 字符" in file_text
    assert "diff_text" not in file_text


def test_readable_tool_content_falls_back_to_original_non_json_text(tmp_path):
    log_path = tmp_path / "model-calls.log"
    model_logging.configure_model_logging(
        stream=io.StringIO(),
        log_path=log_path,
    )

    model_logging.log_model_tool_result(
        request_id="9" * 32,
        call_id="call-plain",
        tool_name="plain_result",
        status="error",
        duration_ms=0.5,
        output_chars=7,
        output_text="第一行\n第二行",
    )

    file_text = log_path.read_text(encoding="utf-8")
    assert "状态：错误" in file_text
    assert "【工具输出】\n第一行\n第二行" in file_text
