from app.tui.state import (
    IssueSeverity,
    StartupIssue,
    TuiHealthState,
)


def test_health_separates_error_status_from_prompt_blocking():
    health = TuiHealthState(
        (
            StartupIssue(
                "skills",
                "Skill catalog unavailable",
                IssueSeverity.ERROR,
                blocks_prompt=False,
            ),
        )
    )

    assert health.has_error_status
    assert health.first_blocking_issue is None


def test_health_returns_first_blocking_issue_in_input_order():
    configuration = StartupIssue(
        "configuration",
        "Invalid provider",
        IssueSeverity.ERROR,
        blocks_prompt=True,
    )
    history = StartupIssue(
        "history",
        "Unable to restore history",
        IssueSeverity.ERROR,
        blocks_prompt=True,
    )

    health = TuiHealthState((configuration, history))

    assert health.first_blocking_issue is configuration


def test_health_can_remove_one_issue_code_without_changing_others():
    history = StartupIssue(
        "history",
        "Unable to restore history",
        IssueSeverity.ERROR,
        blocks_prompt=True,
    )
    workspace = StartupIssue(
        "workspace",
        "Workspace tools unavailable",
        IssueSeverity.ERROR,
        blocks_prompt=True,
    )

    health = TuiHealthState((history, workspace)).without_code("history")

    assert health.issues == (workspace,)
    assert health.first_blocking_issue is workspace


def test_health_replaces_issue_in_place_without_duplicate_code():
    history = StartupIssue(
        "history",
        "Unable to restore history",
        IssueSeverity.ERROR,
        blocks_prompt=True,
    )
    old_skills = StartupIssue(
        "skills",
        "Old skill error",
        IssueSeverity.ERROR,
        blocks_prompt=False,
    )
    new_skills = StartupIssue(
        "skills",
        "New skill error",
        IssueSeverity.ERROR,
        blocks_prompt=False,
    )
    workspace = StartupIssue(
        "workspace",
        "Workspace unavailable",
        IssueSeverity.ERROR,
        blocks_prompt=True,
    )

    health = TuiHealthState((history, old_skills, workspace)).replacing(
        new_skills
    )

    assert health.issues == (history, new_skills, workspace)
