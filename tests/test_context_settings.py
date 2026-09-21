import json

import pytest

from app.runtime.context_settings_store import (
    ContextSettingsConflict,
    ContextSettingsError,
    ContextSettingsStore,
)
from app.runtime.model_budget import ModelBudget, ModelBudgetCatalog
from app.runtime.context_settings import ContextSettings


def test_budget_precedence_and_soft_thresholds():
    catalog = ModelBudgetCatalog({"TUI_CONTEXT_WINDOW_TOKENS": "16000", "LLM_MODEL_BUDGETS": json.dumps([
        {"provider": "deepseek", "model": "demo", "context_window_tokens": 20000}
    ])})
    budget, sources, baseline = catalog.resolve("deepseek", "demo", {
        "compaction": {"recent_turns": 3},
        "models": [{"provider": "deepseek", "model": "demo", "context_window_tokens": 12800}],
    })
    assert budget.input_limit == 4608
    assert budget.trigger_tokens == 3225
    assert budget.target_tokens == 2304
    assert budget.recent_turns == 3
    assert sources["context_window_tokens"] == "settings"
    assert baseline["context_window_tokens"] == 20000
    assert catalog.resolve("aliyun", "other")[0].context_window_tokens == 16000


@pytest.mark.parametrize("values", [
    {"context_window_tokens": 4096}, {"target_percent": 75},
    {"max_output_tokens": True}, {"recent_turns": -1},
    {"summary_cooldown_seconds": 61}, {"summary_output_tokens": 128000},
])
def test_budget_rejects_invalid_combinations(values):
    with pytest.raises(ValueError):
        ModelBudget(**values)


def test_store_persists_private_settings_and_detects_stale_writer(tmp_path):
    store = ContextSettingsStore(tmp_path / "settings.json")
    first = store.load()
    first["compaction"] = {"recent_turns": 4}
    saved = store.save(first, expected_revision=0)
    assert saved["revision"] == 1
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert ContextSettingsStore(store.path).load() == saved
    with pytest.raises(ContextSettingsConflict):
        store.save(first, expected_revision=0)
    saved["compaction"] = {}
    assert store.save(saved, expected_revision=1)["revision"] == 2


def test_corruption_preserves_last_good_but_never_overwrites(tmp_path):
    store = ContextSettingsStore(tmp_path / "settings.json")
    original = store.load()
    store.path.write_text('{"version":1,"version":2}', encoding="utf-8")
    assert store.load(allow_stale=True) == original
    assert store.warning
    with pytest.raises(ContextSettingsError):
        store.save(original, expected_revision=0)
    assert store.path.read_text() == '{"version":1,"version":2}'


def test_settings_service_restores_inheritance_without_changing_other_scope(tmp_path):
    manager = ContextSettings(ModelBudgetCatalog({"TUI_CONTEXT_WINDOW_TOKENS": "64000"}), ContextSettingsStore(tmp_path / "settings.json"))
    first = manager.payload("deepseek", "demo")
    assert first["effective"]["context_window_tokens"] == 64000
    assert first["sources"]["context_window_tokens"] == "legacy_env"
    kwargs = {"provider": "deepseek", "model": "demo", "configured_models": (("aliyun", "other"),)}
    manager.save(expected_revision=0, scope="compaction", overrides={"recent_turns": 3}, **kwargs)
    changed = manager.save(expected_revision=1, scope="model", overrides={"context_window_tokens": 32000}, **kwargs)
    assert changed["effective"]["recent_turns"] == 3
    assert changed["sources"]["context_window_tokens"] == "settings"
    restored = manager.save(expected_revision=2, scope="model", overrides={}, **kwargs)
    assert restored["effective"]["context_window_tokens"] == 64000
    assert restored["effective"]["recent_turns"] == 3
    assert restored["overrides"]["model"] == {}
    assert restored["revision"] == 3
    assert ContextSettings(manager.catalog, ContextSettingsStore(manager.store.path)).payload("deepseek", "demo") == restored


def test_settings_invalid_combination_does_not_publish_or_save(tmp_path):
    manager = ContextSettings(ModelBudgetCatalog({}), ContextSettingsStore(tmp_path / "settings.json"))
    original = manager.snapshot("deepseek", "demo")
    with pytest.raises(ValueError):
        manager.save(expected_revision=0, scope="model", overrides={"context_window_tokens": 4096}, provider="deepseek", model="demo", configured_models=())
    assert not manager.store.path.exists()
    assert manager.snapshot("deepseek", "demo") == original


def test_settings_invalid_combination_on_disk_uses_last_good_but_cold_start_fails(tmp_path):
    manager = ContextSettings(ModelBudgetCatalog({}), ContextSettingsStore(tmp_path / "settings.json"))
    original = manager.snapshot("deepseek", "demo")
    raw = {"version": 1, "revision": 1, "compaction": {"target_percent": 90}, "models": []}
    manager.store.path.write_text(json.dumps(raw), encoding="utf-8")
    fallback = manager.snapshot("deepseek", "demo")
    assert fallback.budget == original.budget
    assert fallback.revision == 0
    assert fallback.warning
    with pytest.raises(ContextSettingsError):
        ContextSettings(manager.catalog, ContextSettingsStore(manager.store.path)).snapshot("deepseek", "demo")
    with pytest.raises(ContextSettingsError):
        manager.save(expected_revision=1, scope="compaction", overrides={}, provider="deepseek", model="demo", configured_models=())


@pytest.mark.parametrize("raw", ["", "[]", "{}", '[{"provider":"deepseek","model":"demo","context_window_tokens":12000,"context_window_tokens":13000}]'])
def test_model_budget_catalog_rejects_explicit_empty_or_duplicate_configuration(raw):
    with pytest.raises(ValueError):
        ModelBudgetCatalog({"LLM_MODEL_BUDGETS": raw})
