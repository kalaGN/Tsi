"""本机 Web 项目配置与旧会话归属迁移测试。"""

import json
import pytest

from app.webui.projects import DEFAULT_PROJECT_ID, WebProjectCatalog, WebProjectError
from app.webui.sessions import WebSessionCatalog


def test_project_catalog_persists_name_and_canonical_path(tmp_path):
    root = tmp_path / "first"
    root.mkdir()
    second = tmp_path / "second"
    second.mkdir()
    path = tmp_path / "web-projects.json"
    catalog = WebProjectCatalog(path, root)

    project = catalog.create("第二个项目", str(second))
    renamed = catalog.update(project.id, name="第二项目", path=str(second))
    restored = WebProjectCatalog(path, root)

    assert catalog.default.path == str(root)
    assert renamed.name == "第二项目"
    assert restored.require(project.id) == renamed
    assert path.stat().st_mode & 0o777 == 0o600


def test_project_catalog_rejects_unsafe_paths_and_corrupt_index(tmp_path):
    root = tmp_path / "first"
    root.mkdir()
    catalog = WebProjectCatalog(tmp_path / "web-projects.json", root)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)

    for path in ("relative", str(tmp_path / "missing"), str(alias), "/", str(root / "..")):
        with pytest.raises(ValueError):
            catalog.create("无效", path)
    with pytest.raises(ValueError):
        catalog.create("重复", str(root))

    catalog.path.write_text('{"version":1,"projects":[],"projects":[]}', encoding="utf-8")
    with pytest.raises(WebProjectError):
        WebProjectCatalog(catalog.path, root)


def test_session_index_v1_migration_preserves_content_and_backup(tmp_path):
    root = tmp_path / "web-sessions"
    catalog = WebSessionCatalog(root)
    first = catalog.current
    store = catalog.session_store(first.id)
    store.save(())
    index = json.loads(catalog.index_path.read_text(encoding="utf-8"))
    index["version"] = 1
    for item in index["sessions"]:
        item.pop("project_id")
    legacy = json.dumps(index, ensure_ascii=False)
    catalog.index_path.write_text(legacy, encoding="utf-8")

    migrated = WebSessionCatalog(root)
    assert migrated.current.project_id == DEFAULT_PROJECT_ID
    assert migrated.session_store(first.id).load() == ()
    assert json.loads(migrated.index_path.read_text(encoding="utf-8"))["version"] == 2
    assert (root / "index.v1.json").read_text(encoding="utf-8") == legacy
    assert WebSessionCatalog(root).current.id == first.id


def test_session_catalog_keeps_project_assignment_when_switching(tmp_path):
    catalog = WebSessionCatalog(tmp_path / "web-sessions")
    initial = catalog.current
    other_project = "1" * 32
    other = catalog.create(other_project)

    assert catalog.current.project_id == other_project
    assert catalog.latest_in_project(DEFAULT_PROJECT_ID).id == initial.id
    assert catalog.latest_in_project(other_project).id == other.id
    catalog.select(initial.id)
    assert catalog.current.project_id == DEFAULT_PROJECT_ID
