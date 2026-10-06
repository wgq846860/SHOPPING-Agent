"""MiMo 薄适配的配置边界；全程离线，不读取项目 .env 或调用模型。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from deepseek_harness import DeepSeekHarness, RunResult

import backend.config as config_module
import backend.agent.harness_client as harness_module
from backend.agent.harness_client import HarnessTurnResult, HarnessUnavailableError, ShoppingHarnessClient
from backend.agent.intent_parser import IntentParseError, parse_intent_with_llm
from backend.config import load_settings


OFFLINE_KEY = "offline-test-placeholder"
MIMO_MODEL = "mimo-v2.6-pro"
MIMO_BASE_URL = "https://api.xiaomimimo.com/v1"


@pytest.fixture(autouse=True)
def isolated_model_configuration(monkeypatch, request):
    # load_settings 的 dotenv 首次加载必须在任何测试调用前关闭。
    monkeypatch.setattr(config_module, "_DOTENV_LOADED", True)
    for name in (
        "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS", "AGENT_BRIDGE_TOKEN", "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    # profile 初始化是外部运行时操作；真实 overlay 组合仍照常执行。
    if request.node.name != "test_bootstrap_shopping_profile_is_idempotent_with_existing_home":
        monkeypatch.setattr(harness_module, "bootstrap_shopping_profile", lambda *_a, **_k: None)


def _configured_settings(tmp_path, monkeypatch, *, provider="xiaomi-mimo"):
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("LLM_MODEL", MIMO_MODEL)
    monkeypatch.setenv("LLM_BASE_URL", MIMO_BASE_URL)
    monkeypatch.setenv("LLM_API_KEY", OFFLINE_KEY)
    return load_settings(project_root=tmp_path, harness_home_name="isolated-home")


def _offline_result():
    return RunResult(
        session_id="offline-session",
        final_response='{"category":"headphones"}',
        finish_reason="stop",
        events=[],
        notifications=[],
    )


class OfflineSdk:
    def start_session(self):
        return self

    def start(self):
        pass

    def close(self):
        pass

    def run(self, _input, *, session_id=None):
        return _offline_result()


def _client(settings, tmp_path, *, factory=None):
    return ShoppingHarnessClient(
        settings,
        sdk_factory=factory or (lambda *_a, **_k: OfflineSdk()),
        overlay_dir=tmp_path / "overlay",
    )


@pytest.mark.parametrize("provider", ["xiaomi-mimo", "Xiaomi-MiMo", "mimo", "MIMO"])
def test_mimo_aliases_start_with_user_model_and_truthful_adapter(tmp_path, monkeypatch, provider):
    """误拒MiMo别名、改写用户模型/地址或冒充adapter都会失败。"""
    settings = _configured_settings(tmp_path, monkeypatch, provider=provider)
    with _client(settings, tmp_path) as client:
        result = client.run_intent_turn("offline configuration check")
        snapshot = client.runtime_config()
        assert result.final_response == '{"category":"headphones"}'
        assert client.started is True
        assert snapshot.provider == provider
        assert snapshot.adapter == "deepseek-official"
        assert snapshot.model == MIMO_MODEL
        assert snapshot.base_url == MIMO_BASE_URL
        assert snapshot.dsh_home == tmp_path / "isolated-home"
        assert snapshot.api_key_present is True
        assert OFFLINE_KEY not in repr(snapshot)


@pytest.mark.parametrize(
    ("missing_variable", "value"),
    [
        ("LLM_MODEL", None), ("LLM_MODEL", ""), ("LLM_MODEL", "   "),
        ("LLM_BASE_URL", None), ("LLM_BASE_URL", ""), ("LLM_BASE_URL", "   "),
    ],
)
def test_mimo_incomplete_configuration_rejected_before_sdk_factory(
    tmp_path, monkeypatch, missing_variable, value,
):
    """MiMo不得回退DeepSeek默认值；配置错误发生在外部操作之前。"""
    settings = _configured_settings(tmp_path, monkeypatch)
    if value is None:
        monkeypatch.delenv(missing_variable)
    else:
        monkeypatch.setenv(missing_variable, value)
    external_calls = []

    def factory(*_args):
        external_calls.append("sdk_factory")
        return OfflineSdk()

    monkeypatch.setattr(
        harness_module, "bootstrap_shopping_profile",
        lambda *_a, **_k: external_calls.append("bootstrap"),
    )
    with _client(settings, tmp_path, factory=factory) as client:
        with pytest.raises(HarnessUnavailableError, match=missing_variable):
            client.run_intent_turn("must not start")
        with pytest.raises(HarnessUnavailableError, match=missing_variable):
            client.runtime_config()
        assert external_calls == []
        assert client.started is False


def test_mimo_overlay_uses_installed_adapter_schema(tmp_path, monkeypatch):
    """MiMo请求预算与关闭思考的设置必须送到实际llm-deepseek插件。"""
    settings = _configured_settings(tmp_path, monkeypatch)
    with _client(settings, tmp_path) as client:
        client.run_intent_turn("offline patch check")
    patch_text = (tmp_path / "overlay" / "shopping.overlay.yml").read_text(encoding="utf-8")
    patch = yaml.safe_load(patch_text)
    llm_entries = [entry for entry in patch if entry.get("id") == "llm-deepseek"]
    assert len(llm_entries) == 1
    llm_config = llm_entries[0]["config"]
    assert llm_config["thinking"] == "disabled"
    assert llm_config["reasoningEffort"] == "off"
    assert llm_config["maxTokens"] == 4096
    assert OFFLINE_KEY not in patch_text


def test_mimo_actual_sdk_constructor_receives_explicit_configuration(tmp_path, monkeypatch, capsys):
    """真实SDK公开config捕捉错误模型/地址/预算传递，不启动运行时。"""
    settings = _configured_settings(tmp_path, monkeypatch)
    constructed_configs = []
    def start_once(sdk):
        if not sdk._initialized:
            constructed_configs.append(sdk.config)
            sdk._initialized = True
    from deepseek_harness.api import Session
    monkeypatch.setattr(DeepSeekHarness, "start", start_once)
    monkeypatch.setattr(Session, "run", lambda self, _input: _offline_result())
    with ShoppingHarnessClient(settings, overlay_dir=tmp_path / "overlay") as client:
        client.run_intent_turn("offline SDK construction")
        snapshot = client.runtime_config()
    assert len(constructed_configs) == 1
    sdk_config = constructed_configs[0]
    assert sdk_config.provider == "deepseek-official"
    assert sdk_config.model == MIMO_MODEL
    assert sdk_config.base_url == MIMO_BASE_URL
    assert sdk_config.api_key == OFFLINE_KEY
    assert sdk_config.max_tokens == 4096
    assert sdk_config.reasoning_effort == "off"
    assert Path(sdk_config.dsh_home) == tmp_path / "isolated-home"
    assert OFFLINE_KEY not in repr(snapshot)
    captured = capsys.readouterr()
    assert OFFLINE_KEY not in captured.out + captured.err


@pytest.mark.parametrize("provider", [None, "deepseek", "DeepSeek", "deepseek-official"])
def test_deepseek_defaults_remain_unchanged(tmp_path, monkeypatch, provider):
    """MiMo分支不能让原有DeepSeek无显式模型/地址配置失效。"""
    if provider is not None:
        monkeypatch.setenv("LLM_PROVIDER", provider)
    settings = load_settings(project_root=tmp_path, harness_home_name="isolated-home")
    constructed_configs = []
    def start_once(sdk):
        if not sdk._initialized:
            constructed_configs.append(sdk.config)
            sdk._initialized = True
    from deepseek_harness.api import Session
    monkeypatch.setattr(DeepSeekHarness, "start", start_once)
    monkeypatch.setattr(Session, "run", lambda self, _input: _offline_result())
    with ShoppingHarnessClient(settings, overlay_dir=tmp_path / "overlay") as client:
        client.run_intent_turn("offline legacy defaults")
        snapshot = client.runtime_config()
    assert snapshot.provider == provider
    assert snapshot.adapter == "deepseek-official"
    assert snapshot.model == "deepseek-v4-flash"
    assert snapshot.base_url is None
    sdk_config = constructed_configs[0]
    assert sdk_config.model == "deepseek-v4-flash"
    assert sdk_config.base_url is None
    assert sdk_config.max_tokens is None
    assert sdk_config.reasoning_effort is None
    patch = yaml.safe_load((tmp_path / "overlay" / "shopping.overlay.yml").read_text(encoding="utf-8"))
    assert all(entry.get("id") != "llm-deepseek" for entry in patch)


def test_unknown_provider_rejected_before_external_operations(tmp_path, monkeypatch):
    settings = _configured_settings(tmp_path, monkeypatch, provider="unsupported-offline-provider")
    external_calls = []

    def factory(*_args):
        external_calls.append("sdk_factory")
        return OfflineSdk()

    monkeypatch.setattr(
        harness_module, "bootstrap_shopping_profile",
        lambda *_a, **_k: external_calls.append("bootstrap"),
    )
    with _client(settings, tmp_path, factory=factory) as client:
        with pytest.raises(HarnessUnavailableError, match="适配器"):
            client.run_intent_turn("unknown provider")
        assert external_calls == []
        assert client.started is False


@pytest.mark.parametrize("finish_reason", ["max-tokens", "length", "max_tokens"])
def test_truncated_turn_rejects_even_complete_valid_intent_json(finish_reason):
    """完整JSON不证明生成完整；达到token上限必须按502拒绝。"""
    payload = {
        "category": "headphones",
        "budget": {"max_cents": None, "currency": "CNY", "basis": "landed_price"},
        "hard_constraints": {
            "stock_required": True, "wireless": None, "active_noise_cancelling": None,
            "microphone": None, "multipoint": None, "open_ear": None, "requires_power": None,
            "opened_return_allowed": None, "sweat_use_allowed": None, "sub_categories": None,
            "allowed_form_factors": None, "excluded_form_factors": None, "ear_tip_sizes": None,
            "min_active_devices": None, "allowed_transports": None, "battery_requirement": None,
            "weight_requirement": None, "device_requirements": None, "item_condition": None,
            "delivery_requirement": None,
        },
        "preferences": {
            "price": 0.0, "battery": 0.0, "weight": 0.0, "comfort": 0.0, "anc": 0.0,
            "sound_quality": 0.0, "call": 0.0, "gaming": 0.0, "portability": 0.0,
            "commute": 0.0, "fit_stability": 0.0, "low_leakage": 0.0, "low_latency": 0.0,
            "shipping": 0.0, "merchant_service": 0.0, "returns": 0.0, "warranty": 0.0,
            "wireless": 0.0, "foldable": 0.0, "fast_charge": 0.0, "tone_match": 0.0,
            "environment_awareness": 0.0,
        },
        "clarifications": [],
    }

    class FakeHarness:
        def __init__(self, reason):
            self.reason = reason

        def run_intent_turn(self, _prompt):
            return HarnessTurnResult(
                session_id="offline-truncation-session",
                final_response=json.dumps(payload),
                finish_reason=self.reason,
            )

    # 同一完整载荷在正常终态合法，排除JSON/字段错误误触502的假阳性。
    complete = parse_intent_with_llm("帮我看看有什么耳机", FakeHarness("stop"))
    assert complete.category == "headphones"
    assert complete.clarifications == []
    with pytest.raises(IntentParseError) as excinfo:
        parse_intent_with_llm("帮我看看有什么耳机", FakeHarness(finish_reason))
    assert excinfo.value.api_status == 502


def test_bootstrap_shopping_profile_is_idempotent_with_existing_home(tmp_path, monkeypatch):
    """真实CLI应重复使用已存在profile；不启动模型session或请求。"""
    for name in (
        "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY",
        "LLM_TIMEOUT_SECONDS", "AGENT_INTENT_MODE", "AGENT_BRIDGE_TOKEN",
        "DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL",
    ):
        monkeypatch.setenv(name, "")
    settings = load_settings(project_root=tmp_path, harness_home_name="isolated-home")
    profile_package = settings.harness_home / "profiles" / "shopping" / "package.json"
    harness_module.bootstrap_shopping_profile(settings)
    assert profile_package.is_file()
    harness_module.bootstrap_shopping_profile(settings)
    assert profile_package.is_file()
