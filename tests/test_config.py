"""配置层测试：路径锚定、静态自检、数据库隔离、LLM_* 环境变量口径。"""

from __future__ import annotations

import dataclasses

import pytest

from backend.config import load_settings


def test_paths_anchor_to_project_root(settings):
    assert settings.project_root.is_dir()
    assert settings.frontend_dir.is_dir()
    assert settings.frontend_dir.name == "frontend"
    assert settings.data_dir.name == "data"


def test_main_and_test_db_are_separate(settings):
    """测试库与演示库必须分离，避免测试污染演示数据（S0.3 验收项）。"""
    assert settings.db_path != settings.test_db_path
    assert settings.db_path.name == "shopping.sqlite3"
    assert settings.test_db_path.name == "shopping_test.sqlite3"
    assert settings.db_path.parent == settings.test_db_path.parent
    # 库目录从 data_dir 派生：隔离 data_dir 即隔离整个数据子树
    assert settings.db_path.parent == settings.data_dir / "db"


def test_db_path_follows_data_dir_override(tmp_path):
    overridden = load_settings(data_dir=tmp_path)
    assert overridden.db_path == tmp_path / "db" / "shopping.sqlite3"
    assert overridden.db_ready() is False


def test_static_files_complete(settings):
    assert settings.static_ok() is True
    assert settings.missing_static() == []


def test_static_missing_reported(tmp_path):
    overridden = load_settings(frontend_dir=tmp_path)
    assert overridden.static_ok() is False
    missing = overridden.missing_static()
    assert "index.html" in missing
    assert "css/main.css" in missing
    assert "js/app.js" in missing
    assert "js/agent.js" in missing


def test_settings_are_readonly(settings):
    with pytest.raises(dataclasses.FrozenInstanceError):
        settings.port = 9999


def test_versions_match_docs(settings):
    """与 docs/api_contract.md（api.v0.1）、docs/data_dictionary.md（commerce.v0.1）对应。"""
    assert settings.api_version == "api.v0.1"
    assert settings.schema_version == "commerce.v0.1"
    assert settings.simulated is True


def test_llm_env_names_align_with_plan(settings):
    """研发计划要求统一使用 LLM_* 命名（本地部署与自定义 API 一节）。"""
    assert settings.llm_provider_env == "LLM_PROVIDER"
    assert settings.llm_base_url_env == "LLM_BASE_URL"
    assert settings.llm_api_key_env == "LLM_API_KEY"
    assert settings.llm_model_env == "LLM_MODEL"
    assert settings.llm_timeout_env == "LLM_TIMEOUT_SECONDS"


def test_llm_values_read_from_env(settings, monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    assert settings.llm_api_key() is None
    assert settings.llm_timeout_seconds() is None

    monkeypatch.setenv("LLM_API_KEY", "  sk-demo-only  ")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "30")
    assert settings.llm_api_key() == "sk-demo-only"  # 前后空白被剥离
    assert settings.llm_timeout_seconds() == 30.0


def test_llm_blank_and_invalid_values(settings, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "   ")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "not-a-number")
    assert settings.llm_api_key() is None
    assert settings.llm_timeout_seconds() is None
