"""S3.1 测试：Agent 工具桥（/api/internal/tools/*，令牌鉴权）。

验收覆盖（s3.md S3.1 第 2 条 + deepseek_harness_integration.md §4）：
- 鉴权：未配置令牌 → 503 桥关闭；错误/缺失令牌 → 403；正确令牌放行；
- 入参：额外键、intent 非法字段（stock_required=false 等）在服务端
  Pydantic 处被拒——工具层 schema 被绕过也不影响；
- 组合工具：SC01 意图 → ok/partial 决策记录（候选数、过滤统计、排序
  权重合计 1、6 位小数分数）；EDGE05 → needs_clarification 不排序；
  EDGE01 → no_results 如实返回；
- 只读工具：详情含 unknown 字段、报价含缺货；不存在商品 404；
- 演示口径：selection_policy=service_first、platform_steered=false、
  无佣金/杠杆字段泄露到决策记录。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.config import load_settings
from backend.database.db import initialize_database
from backend.main import create_app
from backend.schemas.agent import (
    Budget,
    HardConstraints,
    ShoppingIntent,
)
from backend.services import derive

BRIDGE_TOKEN = "test-bridge-token"
FIXED_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=derive.CN_TZ)

HEADERS = {"x-agent-bridge-token": BRIDGE_TOKEN}


@pytest.fixture()
def bridge_app(small_data_dir, tmp_path, monkeypatch):
    """隔离数据库 + 令牌就绪的应用。"""
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", BRIDGE_TOKEN)
    initialize_database(tmp_path / "db" / "shopping.sqlite3", small_data_dir)
    app = create_app(load_settings(data_dir=tmp_path))
    app.state.clock = lambda: FIXED_NOW
    return app


@pytest.fixture()
def bridge_client(bridge_app) -> TestClient:
    return TestClient(bridge_app)


@pytest.fixture()
def locked_app(small_data_dir, tmp_path, monkeypatch):
    """未配置令牌的应用（桥关闭）。"""
    monkeypatch.delenv("AGENT_BRIDGE_TOKEN", raising=False)
    initialize_database(tmp_path / "db" / "shopping.sqlite3", small_data_dir)
    app = create_app(load_settings(data_dir=tmp_path))
    app.state.clock = lambda: FIXED_NOW
    return app


def _sc01_intent() -> dict:
    return ShoppingIntent(
        budget=Budget(max_cents=80000),
        hard_constraints=HardConstraints(wireless=True, active_noise_cancelling=True),
        preferences={"price": 0.2, "anc": 0.8},
    ).model_dump(mode="json")


# ---- 鉴权 ---------------------------------------------------------------------


def test_bridge_disabled_without_token(locked_app):
    client = TestClient(locked_app)
    resp = client.post(
        "/api/internal/tools/recommend_from_intent",
        json={"intent": _sc01_intent()},
        headers=HEADERS,
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "bridge_disabled"


@pytest.mark.parametrize("headers", [None, {}, {"x-agent-bridge-token": "wrong"}])
def test_bridge_rejects_bad_token(bridge_client, headers):
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent",
        json={"intent": _sc01_intent()},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "bridge_forbidden"


# ---- 入参服务端校验 --------------------------------------------------------------


def test_bridge_rejects_extra_keys(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent",
        json={"intent": _sc01_intent(), "sql": "SELECT 1"},
        headers=HEADERS,
    )
    assert resp.status_code == 422


def test_bridge_rejects_intent_that_disables_stock(bridge_client):
    payload = _sc01_intent()
    payload["hard_constraints"]["stock_required"] = False
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": payload}, headers=HEADERS
    )
    assert resp.status_code == 422


def test_bridge_rejects_non_object_intent(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent",
        json={"intent": "not-an-object"},
        headers=HEADERS,
    )
    assert resp.status_code == 422


# ---- 组合工具：SC01 主链路 --------------------------------------------------------


def test_recommend_from_intent_sc01_record(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent",
        json={"intent": _sc01_intent()},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    # 与 S2.1 同锚点：硬约束后候选 P001/P002 → returned_count=2 → partial
    assert body["status"] == "partial"
    assert body["decision_summary"]["candidate_count"] == 2
    assert body["decision_summary"]["returned_count"] == 2
    assert body["selection_policy"] == "service_first"
    assert body["algorithm_version"] == "product-ranking.v1"
    recs = body["recommendations"]
    assert [r["rank"] for r in recs] == [1, 2]
    # S2 排序器既定结果：P002 70.525 > P001 70.275（契约 §7.3 示例同锚点）
    assert [r["product_id"] for r in recs] == ["P002", "P001"]
    assert recs[0]["product_score"] == pytest.approx(70.525, abs=1e-6)
    assert all(r["landed_price_cents"] <= 80000 and r["stock"] > 0 for r in recs)
    assert all(r["platform_steered"] is False for r in recs)
    # 分数保留 6 位小数；权重覆盖全部偏好维度、未表达为 0，合计 1
    product_weights = body["ranking_weights"]["product"]
    assert product_weights["price"] == pytest.approx(0.2) and product_weights["anc"] == pytest.approx(0.8)
    assert sum(product_weights.values()) == pytest.approx(1.0)
    assert sum(body["ranking_weights"]["offer"].values()) == pytest.approx(1.0)
    fields = [r["field"] for r in body["filters_applied"]]
    assert fields[0] == "hard_constraints.wireless"
    assert body["intent"]["budget"]["max_cents"] == 80000
    
    assert "commission" not in resp.text


def test_recommend_from_intent_needs_clarification_edge05(bridge_client):
    intent = ShoppingIntent(
        hard_constraints=HardConstraints(wireless=False, sub_categories=["true_wireless_earbuds"])
    ).model_dump(mode="json")
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": intent}, headers=HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "needs_clarification"
    assert body["recommendations"] == []
    assert body["ranking_weights"] == {"product": {}, "offer": {}}
    assert body["algorithm_version"] is None
    assert body["clarifications"][0]["reason"] == "conflicting_constraints"


def test_recommend_from_intent_no_results_edge01(bridge_client):
    intent = ShoppingIntent(
        budget=Budget(max_cents=10000),
        hard_constraints=HardConstraints(wireless=True, active_noise_cancelling=True),
    ).model_dump(mode="json")
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": intent}, headers=HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "no_results"
    assert body["recommendations"] == []
    assert body["decision_summary"]["candidate_count"] == 0
    assert body["filters_applied"], "零候选也要保留过滤统计用于解释"


# ---- S31-05：date 字段的 JSON 序列化（三个分支都不得 500） ------------------------


def test_bridge_serializes_delivery_date_all_branches(bridge_client, bridge_app):
    """审验反例：intent 含 deadline_date 时三个分支曾因 date 序列化 500。"""
    delivery = {"deadline_date": "2026-10-02", "destination_region": "CN-SH"}

    # no_results 分支（次日请求 → 历史窗口 STALE，零候选）
    no_results = ShoppingIntent(
        budget=Budget(max_cents=60000),
        hard_constraints=HardConstraints(delivery_requirement=delivery),
    ).model_dump(mode="json")
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": no_results}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "no_results"

    # needs_clarification 分支（意图冲突 + 日期字段）
    clarify = ShoppingIntent(
        hard_constraints=HardConstraints(
            wireless=False,
            sub_categories=["true_wireless_earbuds"],
            delivery_requirement=delivery,
        )
    ).model_dump(mode="json")
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": clarify}, headers=HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "needs_clarification"

    # ok 分支：观测日当天时钟 + 宽松截止 → 有候选，日期正常序列化
    bridge_app.state.clock = lambda: datetime(2026, 9, 29, 12, 0, 0, tzinfo=derive.CN_TZ)
    ok = ShoppingIntent(
        budget=Budget(max_cents=60000),
        hard_constraints=HardConstraints(
            delivery_requirement={"deadline_date": "2026-10-20"}
        ),
    ).model_dump(mode="json")
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": ok}, headers=HEADERS
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "2026-10-20" in json.dumps(body)


# ---- S31-09：报价数按 offer_id 去重，方案数独立 -------------------------------------


def test_bridge_eligible_offer_count_dedups_by_offer_id(bridge_client):
    """审验反例：同一 Offer 的多模式方案曾重复计入报价数（33 vs 31）。"""
    intent_payload = ShoppingIntent(
        hard_constraints=HardConstraints(wireless=True)
    ).model_dump(mode="json")
    resp = bridge_client.post(
        "/api/internal/tools/recommend_from_intent", json={"intent": intent_payload}, headers=HEADERS
    )
    assert resp.status_code == 200
    reported = resp.json()["decision_summary"]
    assert reported["eligible_plan_count"] >= reported["eligible_offer_count"]

    # 与 S2.1 候选口径逐一核对（同一固定时钟）
    from backend.database.db import connect
    from backend.services.candidate_service import get_eligible_candidates

    settings = bridge_client.app.state.settings  # type: ignore[attr-defined]
    conn = connect(settings.db_path)
    try:
        search = get_eligible_candidates(
            conn,
            ShoppingIntent(hard_constraints=HardConstraints(wireless=True)),
            now=FIXED_NOW,
        )
        unique_offers = {
            plan.offer["offer_id"]
            for candidate in search.candidates
            for plan in candidate.eligible_plans
        }
        plan_count = sum(len(c.eligible_plans) for c in search.candidates)
    finally:
        conn.close()
    assert reported["eligible_offer_count"] == len(unique_offers)
    assert reported["eligible_plan_count"] == plan_count


# ---- 只读工具 -------------------------------------------------------------------


def test_get_product_details_returns_unknown_as_null(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/get_product_details",
        json={"product_id": "P002"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["product"]["product_id"] == "P002"
    # P002 无续航/重量记录：未知保持 null，不虚构
    assert body["product"]["battery_specs"] is None
    assert body["metrics"]["product_id"] == "P002"
    assert body["data_version"] == "small-v1"


def test_get_product_details_not_found(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/get_product_details",
        json={"product_id": "P9999"},
        headers=HEADERS,
    )
    assert resp.status_code == 404


def test_get_product_offers_includes_out_of_stock(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/get_product_offers",
        json={"product_id": "P015"},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert all(item["offer"]["stock"] == 0 for item in body["items"])


def test_get_product_offers_invalid_mode(bridge_client):
    resp = bridge_client.post(
        "/api/internal/tools/get_product_offers",
        json={"product_id": "P001", "mode_id": "nonexistent"},
        headers=HEADERS,
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_mode"
