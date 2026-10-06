"""启动器状态文件读写测试（.runtime/launcher/config.json，不上云）。"""

from __future__ import annotations

from scripts.launcher.state import DEFAULT_STATE, load_state, save_state, state_path


def test_roundtrip(tmp_path):
    save_state(tmp_path, port=8010, last_mode="llm")
    assert load_state(tmp_path) == {"port": 8010, "last_mode": "llm"}


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    path = state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken json", encoding="utf-8")
    assert load_state(tmp_path) == dict(DEFAULT_STATE)


def test_invalid_and_unknown_values_dropped(tmp_path):
    save_state(tmp_path, port=0, last_mode="bogus", remote_url="x", unknown=1)
    assert load_state(tmp_path) == dict(DEFAULT_STATE)


def test_save_merges_not_replaces(tmp_path):
    save_state(tmp_path, port=9000)
    save_state(tmp_path, last_mode="rule_based_demo")
    state = load_state(tmp_path)
    assert state["port"] == 9000
    assert state["last_mode"] == "rule_based_demo"
