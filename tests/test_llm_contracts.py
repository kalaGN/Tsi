import pytest

from app.services.llm.contracts import ModelStep, TokenUsage


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
