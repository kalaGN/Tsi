import pytest

from app.services.llm.contracts import ModelOption, ModelStep, TokenUsage


def test_model_option_contains_only_safe_selection_metadata():
    option = ModelOption("deepseek", "deepseek-v4-flash", True)

    assert option.provider == "deepseek"
    assert option.model == "deepseek-v4-flash"
    assert option.api_key_configured is True


@pytest.mark.parametrize(
    "values",
    [
        ("", "model", True),
        ("unknown", "model", True),
        ("deepseek", "", True),
        ("deepseek", " model ", True),
        ("deepseek", "bad\nmodel", True),
        ("deepseek", "x" * 129, True),
        ("deepseek", "model", 1),
    ],
)
def test_model_option_rejects_invalid_metadata(values):
    with pytest.raises(ValueError):
        ModelOption(*values)


def test_token_usage_accepts_zero_and_adds_each_dimension():
    left = TokenUsage(input_tokens=0, output_tokens=2, total_tokens=2)
    right = TokenUsage(input_tokens=3, output_tokens=4, total_tokens=7)

    assert left + right == TokenUsage(3, 6, 9)


@pytest.mark.parametrize(
    "values",
    [
        (-1, 0, -1),
        (True, 0, 1),
        (1.0, 0, 1),
        ("1", 0, 1),
        (1, 2, 4),
    ],
)
def test_token_usage_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        TokenUsage(*values)


def test_model_step_keeps_usage_optional_for_existing_providers():
    step = ModelStep(200, "done", ())

    assert step.token_usage is None
