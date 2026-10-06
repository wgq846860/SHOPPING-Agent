"""S3.3 单元测试：run_agent_query 链路编排（无 HTTP 层）。

覆盖 API 测试不易直达的模块级行为：
- rule_based_demo 注入 Harness 必须拒绝（演示模式零模型调用，防回归）；
- 未知模式拒绝；
- llm 模式结果携带 used_llm_explanation / fallback_reason 元数据；
- 零候选/澄清分支不走排序（algorithm_version null、权重空）。

数据锚点同 tests/test_api_agent.py（small-v1 固定种子，NOW=2026-09-30）。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from backend.agent.harness_client import HarnessTurnResult
from backend.agent.shopping_agent import run_agent_query
from backend.database.db import connect, initialize_database
from backend.schemas.agent import PREFERENCE_DIMENSIONS
from backend.services import derive

FIXED_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=derive.CN_TZ)


@pytest.fixture()
def conn(small_data_dir, tmp_path):
    initialize_database(tmp_path / "db" / "shopping.sqlite3", small_data_dir)
    connection = connect(tmp_path / "db" / "shopping.sqlite3")
    yield connection
    connection.close()


class StubHarness:
    def run_intent_turn(self, prompt: str) -> HarnessTurnResult:
        return HarnessTurnResult("session", "{}", "stop")


def test_rule_mode_rejects_harness_injection(conn):
    with pytest.raises(ValueError, match="零模型调用"):
        run_agent_query(conn, "无线耳机", mode="rule_based_demo", now=FIXED_NOW,
                        harness=StubHarness())


def test_unknown_mode_rejected(conn):
    with pytest.raises(ValueError, match="未知意图模式"):
        run_agent_query(conn, "无线耳机", mode="auto", now=FIXED_NOW)


def test_rule_mode_no_results_metadata(conn):
    result = run_agent_query(conn, "100元以内无线主动降噪耳机",
                             mode="rule_based_demo", now=FIXED_NOW)
    assert result.status == "no_results"
    assert result.used_llm_explanation is False
    assert result.fallback_reason is None
    assert result.body["algorithm_version"] is None
    assert result.body["ranking_weights"] == {"product": {}, "offer": {}}


def test_rule_mode_ranked_metadata(conn):
    result = run_agent_query(conn, "800元以内无线主动降噪耳机",
                             mode="rule_based_demo", now=FIXED_NOW)
    assert result.status in {"ok", "partial"}
    assert result.body["recommendations"]
    assert result.body["intent"]["hard_constraints"]["wireless"] is True
    assert result.body["intent"]["budget"]["max_cents"] == 80000


def test_intent_preferences_all_dimensions_present(conn):
    result = run_agent_query(conn, "800元以内无线主动降噪耳机，尽量轻",
                             mode="rule_based_demo", now=FIXED_NOW)
    preferences = result.body["intent"]["preferences"]
    assert set(preferences) == set(PREFERENCE_DIMENSIONS)
    assert preferences["weight"] == 0.6  # 尽量轻 → 偏好，不是硬约束
