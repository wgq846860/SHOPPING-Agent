"""服务托管纯逻辑测试：子进程 env 注入、netstat 解析、健康探测降级。"""

from __future__ import annotations

import socket

import pytest

from scripts.launcher.service import build_child_env, fetch_health, parse_netstat_listening

NETSTAT_SAMPLE = """\
  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       23320
  TCP    127.0.0.1:8000         127.0.0.1:51000        ESTABLISHED     23320
  TCP    0.0.0.0:8001           0.0.0.0:0              LISTENING       999
  TCP    [::1]:8000             [::]:0                 LISTENING       23320
"""


def test_build_child_env_injects_mode_without_mutating_base():
    base = {"PATH": "x", "AGENT_INTENT_MODE": "rule_based_demo", "PYTHONUTF8": "0"}
    env = build_child_env("llm", base)
    assert env["AGENT_INTENT_MODE"] == "llm"
    assert env["PYTHONUTF8"] == "1"
    assert env["PATH"] == "x"
    assert base["AGENT_INTENT_MODE"] == "rule_based_demo"


def test_build_child_env_rejects_unknown_mode():
    with pytest.raises(ValueError):
        build_child_env("demo", base={})


def test_parse_netstat_listening():
    assert parse_netstat_listening(NETSTAT_SAMPLE, 8000) == [23320]
    assert parse_netstat_listening(NETSTAT_SAMPLE, 8001) == [999]
    assert parse_netstat_listening(NETSTAT_SAMPLE, 9999) == []


def test_fetch_health_none_on_closed_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert fetch_health(port, timeout=0.5) is None
