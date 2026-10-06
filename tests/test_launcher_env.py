"""启动器配置视图纯逻辑测试：合并语义、打码、向导触发、.env 写入。"""

from __future__ import annotations

import pytest

from scripts.launcher.config_view import (
    FIELDS,
    effective_values,
    load_env_overview,
    mask_secret,
    needs_setup,
    required_for_mode,
    write_env_file,
)


@pytest.fixture(autouse=True)
def clean_launcher_env(monkeypatch):
    """隔离用户真实 .env/进程变量：launcher 测试不读、不依赖真实密钥。"""
    for name in FIELDS:
        monkeypatch.delenv(name, raising=False)


def _write_env(tmp_path, content):
    path = tmp_path / ".env"
    path.write_text(content, encoding="utf-8")
    return path


def test_mask_secret():
    assert mask_secret("") == "未设置"
    assert mask_secret("   ") == "未设置"
    assert mask_secret("abcd") == "已设置（****）"
    assert mask_secret("sk-12345678") == "已设置（****5678）"


def test_effective_values_env_overrides_file(tmp_path, monkeypatch):
    _write_env(tmp_path, "LLM_API_KEY=filekey\nLLM_MODEL=m1\n")
    monkeypatch.setenv("LLM_API_KEY", "envkey")
    values = effective_values(tmp_path)
    assert values["LLM_API_KEY"] == "envkey"
    assert values["LLM_MODEL"] == "m1"
    assert "LLM_PROVIDER" not in values


def test_effective_values_without_file(tmp_path):
    assert effective_values(tmp_path) == {}


def test_load_env_overview_masks_secrets(tmp_path):
    _write_env(tmp_path, "LLM_API_KEY=sk-12345678\nLLM_MODEL=mimo\n")
    rows = {row[0]: row for row in load_env_overview(tmp_path)}
    assert rows["LLM_API_KEY"][1] == "已设置（****5678）"
    assert rows["LLM_API_KEY"][2] == ".env"
    assert rows["LLM_MODEL"][1] == "mimo"
    assert rows["AGENT_INTENT_MODE"][1] == "（未设置）"
    assert rows["AGENT_INTENT_MODE"][2] == "未设置"


def test_load_env_overview_env_source(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "120")
    rows = {row[0]: row for row in load_env_overview(tmp_path)}
    assert rows["LLM_TIMEOUT_SECONDS"][1:] == ("120", "进程环境变量")


def test_needs_setup():
    assert needs_setup({})
    assert needs_setup({"LLM_API_KEY": "  "})
    assert not needs_setup({"LLM_API_KEY": "x"})


def test_required_for_mode():
    assert required_for_mode("llm") == [
        "LLM_API_KEY",
        "LLM_PROVIDER",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "AGENT_BRIDGE_TOKEN",
    ]
    assert required_for_mode("rule_based_demo") == []


def test_write_env_file_backs_up_and_merges(tmp_path):
    path = _write_env(tmp_path, "# 注释\nKEEP=1\nLLM_MODEL=old\n")
    backup = write_env_file(path, {"LLM_MODEL": "new", "LLM_PROVIDER": "mimo"})
    assert backup is not None and backup.is_file()
    assert "LLM_MODEL=old" in backup.read_text(encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    assert "# 注释" in text
    assert "KEEP=1" in text
    assert "LLM_MODEL=new" in text
    assert "LLM_MODEL=old" not in text
    assert "LLM_PROVIDER=mimo" in text


def test_write_env_file_fresh(tmp_path):
    path = tmp_path / ".env"
    assert write_env_file(path, {"LLM_API_KEY": "k"}) is None
    assert path.read_text(encoding="utf-8") == "LLM_API_KEY=k\n"
