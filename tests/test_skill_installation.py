import asyncio
import base64
import json

import httpx
import pytest

import tools.skill_installation as skill_installation
from tools import ToolCall, ToolExecutionContext, ToolRegistry
from tools.contracts import (
    SkillInstallApprovalRequest,
    ToolErrorCode,
    ToolRejectedError,
)
from tools.skill_installation import (
    GitHubContentsFetcher,
    InstallSkillTool,
    SkillInstaller,
    SkillInstallRequest,
    parse_github_skill_url,
    parse_install_request,
)


def write_skill(root, directory="source-folder", name="demo-skill", body="# Demo\n"):
    skill = root / directory
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 演示技能\n---\n\n{body}",
        encoding="utf-8",
    )
    return skill


def install_call(source="source-folder", name="demo-skill"):
    return ToolCall(
        "install-1",
        "install_skill",
        json.dumps(
            {
                "source_type": "codex_home",
                "source": source,
                "expected_name": name,
            }
        ),
    )


def test_install_request_strictly_separates_supported_sources():
    local = parse_install_request(
        {
            "source_type": "codex_home",
            "source": "skill-using-superpowers",
            "expected_name": "using-superpowers",
        }
    )
    assert local.source_display == "~/.codex/skills/skill-using-superpowers"

    github = parse_github_skill_url(
        "https://github.com/openai/skills/tree/main/skills/demo"
    )
    assert (github.owner, github.repository, github.ref, github.directory) == (
        "openai",
        "skills",
        "main",
        "skills/demo",
    )

    invalid = (
        "http://github.com/openai/skills/tree/main/skills/demo",
        "https://evil.example/openai/skills/tree/main/skills/demo",
        "https://github.com/openai/skills/blob/main/skills/demo",
        "https://github.com/openai/skills/tree/main",
        "https://github.com/openai/skills/tree/main/skills/demo?token=x",
    )
    for value in invalid:
        with pytest.raises(Exception):
            parse_github_skill_url(value)


def test_install_preview_has_no_source_or_project_side_effect(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "missing-codex-skills"
    tool = InstallSkillTool(
        SkillInstaller(project, lambda _catalog: 2, codex_skills_root=codex)
    )

    request = _run(tool.preview("call", json.loads(install_call().arguments_json)))

    assert isinstance(request, SkillInstallApprovalRequest)
    assert request.target_path == ".agents/skills/demo-skill"
    assert request.network_access is False
    assert not (project / ".agents").exists()


def test_local_skill_install_requires_approval_and_publishes_catalog(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "codex-skills"
    write_skill(codex)
    published = []
    tool = InstallSkillTool(
        SkillInstaller(
            project,
            lambda catalog: published.append(catalog) or 2,
            codex_skills_root=codex,
        )
    )
    registry = ToolRegistry((tool,))

    denied = _run(registry.execute(install_call()))
    assert json.loads(denied.output)["error"]["code"] == "approval_unavailable"
    assert not (project / ".agents").exists()

    approvals = []

    async def approve(request):
        approvals.append(request)
        return True

    result = _run(
        registry.execute(
            install_call(),
            ToolExecutionContext(approval_handler=approve),
        )
    )
    payload = json.loads(result.output)["data"]
    assert payload["active_from"] == "next_request"
    assert payload["runtime_version"] == 2
    assert (project / ".agents/skills/demo-skill/SKILL.md").is_file()
    assert published[0].count == 1
    assert approvals[0].source_display == "~/.codex/skills/source-folder"


def test_local_install_rejects_symlink_and_existing_target(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "codex-skills"
    source = write_skill(codex)
    (source / "unsafe").symlink_to(source / "SKILL.md")
    tool = InstallSkillTool(
        SkillInstaller(project, lambda _catalog: 2, codex_skills_root=codex)
    )
    registry = ToolRegistry((tool,))

    async def approve(_request):
        return True

    invalid = _run(
        registry.execute(
            install_call(), ToolExecutionContext(approval_handler=approve)
        )
    )
    assert json.loads(invalid.output)["error"]["code"] == "skill_package_invalid"
    assert not (project / ".agents/skills/demo-skill").exists()

    (source / "unsafe").unlink()
    target = project / ".agents/skills/demo-skill"
    target.mkdir(parents=True)
    conflict = _run(
        registry.execute(
            install_call(), ToolExecutionContext(approval_handler=approve)
        )
    )
    assert json.loads(conflict.output)["error"]["code"] == "skill_already_exists"


def test_refresh_failure_rolls_back_new_target(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "codex-skills"
    write_skill(codex)

    def fail_publish(_catalog):
        raise RuntimeError("private state")

    registry = ToolRegistry(
        (
            InstallSkillTool(
                SkillInstaller(project, fail_publish, codex_skills_root=codex)
            ),
        )
    )

    async def approve(_request):
        return True

    result = _run(
        registry.execute(
            install_call(), ToolExecutionContext(approval_handler=approve)
        )
    )
    assert json.loads(result.output)["error"]["code"] == "skill_refresh_failed"
    assert not (project / ".agents/skills/demo-skill").exists()
    assert not tuple(project.glob(".tsi-skill-install-*"))


def test_install_rejects_symlinked_project_agents_parent(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / ".agents").symlink_to(outside, target_is_directory=True)
    codex = tmp_path / "codex-skills"
    write_skill(codex)
    registry = ToolRegistry(
        (
            InstallSkillTool(
                SkillInstaller(
                    project,
                    lambda _catalog: 2,
                    codex_skills_root=codex,
                )
            ),
        )
    )

    async def approve(_request):
        return True

    result = _run(
        registry.execute(
            install_call(), ToolExecutionContext(approval_handler=approve)
        )
    )

    assert json.loads(result.output)["error"]["code"] == "skill_refresh_failed"
    assert not (outside / "skills").exists()


def test_concurrent_same_name_install_has_only_one_success(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    codex = tmp_path / "codex-skills"
    write_skill(codex)
    installer = SkillInstaller(
        project,
        lambda _catalog: 2,
        codex_skills_root=codex,
    )
    request = SkillInstallRequest(
        "codex_home",
        "source-folder",
        "demo-skill",
        "~/.codex/skills/source-folder",
    )

    async def scenario():
        return await asyncio.gather(
            installer.install(request),
            installer.install(request),
            return_exceptions=True,
        )

    results = _run(scenario())
    successes = [result for result in results if isinstance(result, dict)]
    failures = [result for result in results if isinstance(result, ToolRejectedError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code is ToolErrorCode.SKILL_ALREADY_EXISTS


def test_github_fetcher_uses_fixed_api_and_decodes_skill(tmp_path):
    seen = []
    markdown = b"---\nname: demo-skill\ndescription: Demo\n---\n"

    def handler(request):
        seen.append(request)
        assert request.url.host == "api.github.com"
        assert "authorization" not in request.headers
        if request.url.path.endswith("/contents/skills/demo"):
            return httpx.Response(
                200,
                json=[{"name": "SKILL.md", "type": "file"}],
            )
        return httpx.Response(
            200,
            json={
                "name": "SKILL.md",
                "type": "file",
                "encoding": "base64",
                "content": base64.encodebytes(markdown).decode(),
            },
        )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ) as client:
            fetcher = GitHubContentsFetcher(client)
            request = parse_install_request(
                {
                    "source_type": "github",
                    "source": "https://github.com/acme/repo/tree/main/skills/demo",
                    "expected_name": "demo-skill",
                }
            )
            await fetcher.fetch(request, tmp_path / "candidate")

    _run(scenario())
    assert (tmp_path / "candidate/SKILL.md").read_bytes() == markdown
    assert len(seen) == 2


def test_github_install_uses_injected_fetcher_and_publishes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    fetched = []
    published = []

    class FakeGitHubFetcher:
        async def fetch(self, request, destination):
            fetched.append(request.source_display)
            destination.mkdir(parents=True)
            (destination / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: Demo\n---\n",
                encoding="utf-8",
            )

    registry = ToolRegistry(
        (
            InstallSkillTool(
                SkillInstaller(
                    project,
                    lambda catalog: published.append(catalog) or 2,
                    github_fetcher=FakeGitHubFetcher(),
                )
            ),
        )
    )
    call = ToolCall(
        "github-install",
        "install_skill",
        json.dumps(
            {
                "source_type": "github",
                "source": "https://github.com/acme/repo/tree/main/skills/demo",
                "expected_name": "demo-skill",
            }
        ),
    )

    async def approve(request):
        assert request.network_access is True
        return True

    result = _run(
        registry.execute(call, ToolExecutionContext(approval_handler=approve))
    )

    assert result.is_error is False
    assert fetched == ["https://github.com/acme/repo/tree/main/skills/demo"]
    assert published[0].count == 1
    assert (project / ".agents/skills/demo-skill/SKILL.md").is_file()


def test_github_fetcher_applies_one_total_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(skill_installation, "GITHUB_TIMEOUT_SECONDS", 0.01)

    async def handler(_request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=[])

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), trust_env=False
        ) as client:
            fetcher = GitHubContentsFetcher(client)
            request = parse_install_request(
                {
                    "source_type": "github",
                    "source": "https://github.com/acme/repo/tree/main/skills/demo",
                    "expected_name": "demo-skill",
                }
            )
            with pytest.raises(ToolRejectedError) as captured:
                await fetcher.fetch(request, tmp_path / "candidate")
            assert captured.value.code is ToolErrorCode.SKILL_DOWNLOAD_TIMEOUT

    _run(scenario())


def _run(awaitable):
    return asyncio.run(awaitable)
