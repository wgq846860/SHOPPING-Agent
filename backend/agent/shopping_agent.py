"""Public Demo implementation."""

from __future__ import annotations

import dataclasses
from copy import deepcopy
from datetime import datetime

from backend.agent.intent_parser import IntentParseError, parse_intent
from backend.config import DEFAULT_DESTINATION_REGION
from backend.ranking.offer_ranker import recommend
from backend.schemas.agent import ShoppingIntent
from backend.schemas.common import Source
from backend.services import derive
from backend.services.candidate_service import CandidateSearch, get_eligible_candidates
from backend.services.errors import DataError
from backend.services.explanation_service import (
    ExplainedRecommendation,
    ExplanationReport,
    FactStatement,
    build_explanations,
)
from backend.agent.explainer import ExplanationOutcome, explain_recommendations
from backend.services.product_service import get_product_summary

DEMO_SELECTION_POLICY = "service_first"

#: 演示数据固定目的地（本版唯一白名单地区）。
_DEMO_REGION = DEFAULT_DESTINATION_REGION


class AgentQueryResult:
    """run_agent_query 的产物：契约响应体（不含 meta）与解释层元数据。

    status/见契约 §7.2；used_llm_explanation 与 fallback_reason 供日志与
    测试观察，不进公开响应。
    """

    def __init__(
        self,
        *,
        status: str,
        body: dict,
        intent: ShoppingIntent,
        used_llm_explanation: bool,
        fallback_reason: str | None,
        data_version: str | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.intent = intent
        self.used_llm_explanation = used_llm_explanation
        self.fallback_reason = fallback_reason
        self.data_version = data_version


@dataclasses.dataclass(frozen=True)
class PreparedDecision:
    """短读事务内完成的事实快照；语言回合只替换其受控解释文本。"""

    intent: ShoppingIntent
    body: dict
    report: ExplanationReport | None
    recommendations: list
    data_version: str | None


def prepare_decision(conn, intent: ShoppingIntent, *, mode: str, now: datetime) -> PreparedDecision:
    from backend.database.queries import fetch_manifest

    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        manifest = fetch_manifest(conn)
        version = manifest.get("data_version") if manifest else None
        search = get_eligible_candidates(conn, intent, now=now)
        if search.status == "needs_clarification":
            return PreparedDecision(intent, _clarification_body(intent, mode, search), None, [], version)
        recs = recommend(search.candidates, intent, selection_policy=DEMO_SELECTION_POLICY) if search.candidates else []
        report = build_explanations(recs, intent, search=search, data_version=version)
        body = (_ranked_body(conn, mode, intent, search, report, now, []) if recs
                else _no_results_body(intent, mode, search, report))
        return PreparedDecision(intent, body, report, recs, version)
    finally:
        if owns_transaction:
            conn.rollback()  # 模型等待期间不占用 SQLite 读锁，允许换批次。


def run_agent_query(
    conn,
    query: str,
    *,
    mode: str,
    now: datetime,
    harness=None,
    decision_runner=None,
) -> AgentQueryResult:
    """一次购物请求的完整链路：解析 → 过滤 → 排序 → 解释 → 组装。

    harness 仅供 llm 模式使用（解析/购物工具/语言解释各为独立session）；
    decision_runner 返回同请求工具保存的权威快照。规则模式直接执行
    prepare_decision，且必须传 harness=None，保证零模型调用。
    IntentParseError 由调用方映射到 api.v0.1 错误信封；DataError 向上
    传播（数据不可读 → 503）。
    """
    if mode == "rule_based_demo" and harness is not None:
        raise ValueError("rule_based_demo 模式不得注入 Harness（演示模式零模型调用）")
    if mode not in ("llm", "rule_based_demo"):
        raise ValueError(f"未知意图模式: {mode}")

    intent = parse_intent(query, mode=mode, harness=harness)
    if mode == "llm" and not intent.clarifications:
        if decision_runner is None:
            raise IntentParseError("invalid_output", "本次请求未取得购物工具决策记录",
                                   [{"field": "query", "reason": "missing_decision_tool"}])
        prepared = decision_runner(intent)
    else:
        prepared = prepare_decision(conn, intent, mode=mode, now=now)
    body = deepcopy(prepared.body)
    report = prepared.report
    recs = prepared.recommendations

    used_llm = False
    fallback_reason = None
    extra_warnings: list[dict] = []
    if mode == "llm" and harness is not None and recs:
        outcome: ExplanationOutcome = explain_recommendations(report, intent, recs, harness)
        report = outcome.report
        used_llm = outcome.used_llm
        fallback_reason = outcome.fallback_reason
        extra_warnings = list(outcome.warnings)
        for payload, entry in zip(body["recommendations"], report.recommendations, strict=True):
            for name in ("reason", "strengths", "weaknesses", "trade_offs"):
                payload[name] = [_statement_payload(s) for s in getattr(entry, name)]
        body["warnings"] += extra_warnings
    return AgentQueryResult(
        status=body["status"], body=body, intent=intent,
        used_llm_explanation=used_llm, fallback_reason=fallback_reason,
        data_version=prepared.data_version,
    )


# ---------------------------------------------------------------------------
# 未排序分支（澄清 / 零候选）
# ---------------------------------------------------------------------------


def _base_body(intent: ShoppingIntent, mode: str) -> dict:
    return {
        "status": None,
        "intent": intent.model_dump(mode="json"),
        "recommendations": [],
        "decision_summary": None,
        "filters_applied": [],
        "ranking_weights": {"product": {}, "offer": {}},
        "mode": mode,
        "algorithm_version": None,
        "warnings": [],
    }


def _clarification_body(intent: ShoppingIntent, mode: str, search: CandidateSearch) -> dict:
    body = _base_body(intent, mode)
    body["status"] = "needs_clarification"
    body["decision_summary"] = {
        "candidate_count": 0,
        "eligible_offer_count": 0,
        "returned_count": 0,
        "comparison_scope": "未检索",
        "message": "需求存在冲突或缺少必要信息，请先确认澄清项。",
        "relaxations": [],
    }
    body["warnings"] = [_simulated_warning()]
    return body


def _no_results_body(
    intent: ShoppingIntent,
    mode: str,
    search: CandidateSearch,
    report: ExplanationReport,
) -> dict:
    body = _base_body(intent, mode)
    body["status"] = "no_results"
    body["decision_summary"] = _decision_summary(report.decision_summary)
    body["filters_applied"] = _filters(search)
    body["warnings"] = list(report.warnings)
    return body


# ---------------------------------------------------------------------------
# 已排序分支
# ---------------------------------------------------------------------------


def _ranked_body(
    conn,
    mode: str,
    intent: ShoppingIntent,
    search: CandidateSearch,
    report: ExplanationReport,
    now: datetime,
    extra_warnings: list[dict],
) -> dict:
    top = report.recommendations
    body = _base_body(intent, mode)
    body["status"] = "ok" if len(top) >= 3 else "partial"
    body["recommendations"] = [
        _recommendation_payload(conn, entry, now) for entry in top
    ]
    body["decision_summary"] = _decision_summary(report.decision_summary)
    body["filters_applied"] = _filters(search)
    first = top[0]
    body["ranking_weights"] = {
        "product": dict(first.product_score.weights),
        "offer": dict(first.recommended.weights),
    }
    body["algorithm_version"] = first.product_score.algorithm_version
    body["warnings"] = list(report.warnings) + extra_warnings
    return body


def _decision_summary(summary) -> dict:
    """契约 §7.2 DecisionSummary 固定 6 键；方案计数不进公开响应。"""
    return {
        "candidate_count": summary.candidate_count,
        "eligible_offer_count": summary.eligible_offer_count,
        "returned_count": summary.returned_count,
        "comparison_scope": summary.comparison_scope,
        "message": summary.message,
        "relaxations": list(summary.relaxations),
    }


def _filters(search: CandidateSearch) -> list[dict]:
    return [dataclasses.asdict(record) for record in search.filters_applied]


def _simulated_warning() -> dict:
    return {
        "code": "SIMULATED_DATA",
        "message": "演示数据为模拟来源，分数与理由仅供流程验证。",
        "field": None,
        "product_id": None,
    }


# ---------------------------------------------------------------------------
# 单条推荐组装（商品摘要复用 S1.3 出口；报价/评分/解释来自本次决策记录）
# ---------------------------------------------------------------------------


def _recommendation_payload(conn, entry: ExplainedRecommendation, now: datetime) -> dict:
    score = entry.product_score
    summary = get_product_summary(conn, score.product_id, region=_DEMO_REGION, now=now)
    if summary is None:
        # 同一连接同一快照下候选商品必在库；缺失说明决策记录与数据库不一致
        raise DataError(f"商品 {score.product_id} 在决策记录中存在但数据库缺失")
    return {
        "rank": entry.rank,
        "product": summary.model_dump(),
        "offer": _offer_payload(entry),
        "selected_specs": dict(score.selected_specs),
        "score": score.score,
        "score_breakdown": {
            "product": [_component_payload(c) for c in score.components],
            "offer": [_component_payload(c) for c in entry.recommended.components],
        },
        "reason": [_statement_payload(s) for s in entry.reason],
        "strengths": [_statement_payload(s) for s in entry.strengths],
        "weaknesses": [_statement_payload(s) for s in entry.weaknesses],
        "trade_offs": [_statement_payload(s) for s in entry.trade_offs],
    }


def _offer_payload(entry: ExplainedRecommendation) -> dict:
    offer = entry.recommended
    plan = offer.plan
    raw = plan.offer
    policies = plan.policies
    return {
        "offer_id": offer.offer_id,
        "product_id": offer.product_id,
        "merchant_id": offer.merchant_id,
        "merchant_name": plan.merchant.get("merchant_name"),
        "mode_id": offer.mode_id,
        "currency": raw.get("currency"),
        "price_cents": raw.get("price_cents"),
        "shipping_fee_cents": raw.get("shipping_fee_cents"),
        "accessory_total_cents": plan.accessory_total,
        "instant_discount_cents": raw.get("instant_discount_cents"),
        "landed_price_cents": offer.landed,
        "stock": raw.get("stock"),
        "item_condition": raw.get("item_condition"),
        "delivery_window": plan.delivery_window.model_dump(),
        "effective_return_policy": policies.return_policy.model_dump(),
        "effective_warranty": policies.warranty.model_dump(),
        "policy_origin": policies.policy_origin,
        "warranty_origin": policies.warranty_origin,
        "score": offer.score,
        "field_sources": _offer_field_sources(plan),
        "derivation": _offer_derivation(plan),
    }


def _offer_field_sources(plan: derive.OfferPlan) -> dict[str, dict]:
    """RecommendedOffer 返回的原始叶子逐字段来源（对齐 §1.1 展开规则）。"""
    offer_fs = plan.offer.get("field_sources") or {}
    merchant_fs = plan.merchant.get("field_sources") or {}
    sources: dict[str, Source] = {}
    for key in ("price_cents", "shipping_fee_cents", "instant_discount_cents",
                "stock", "item_condition"):
        if offer_fs.get(key):
            sources[key] = Source(**offer_fs[key])
    if merchant_fs.get("merchant_name"):
        sources["merchant_name"] = Source(**merchant_fs["merchant_name"])
    for target, key in (
        ("delivery_window.destination_region", "delivery.destination_region"),
        ("delivery_window.commitment_type", "delivery.commitment_type"),
        ("delivery_window.observed_at", "delivery.max_days"),
    ):
        if offer_fs.get(key):
            sources[target] = Source(**offer_fs[key])
    for key, source in plan.policies.field_sources.items():
        sources[key] = source
    return {key: value.model_dump() for key, value in sources.items()}


def _offer_derivation(plan: derive.OfferPlan) -> dict:
    offer_id = plan.offer["offer_id"]
    derivations = {
        "landed_price_cents": {
            "rule": "landed_price.v1",
            "input_refs": [
                f"{offer_id}.price_cents",
                f"{offer_id}.shipping_fee_cents",
                f"{offer_id}.instant_discount_cents",
                f"{offer_id}.accessories",
            ],
        },
        "accessory_total_cents": {
            "rule": "accessory_total.v1",
            "input_refs": [
                f"{offer_id}.accessories",
                f"{plan.offer['product_id']}.connection_modes",
            ],
        },
        "delivery_window": plan.delivery_derivation.model_dump(),
    }
    for key, derivation in plan.policies.derivations.items():
        derivations[key] = derivation.model_dump()
    return derivations


def _component_payload(component) -> dict:
    """Public Demo implementation."""
    return {
        "dimension": component.dimension,
        "raw_value": component.raw_value,
        "unit": component.unit,
        "basis": component.basis,
        "normalized_score": component.normalized_score,
        "weight": component.weight,
        "contribution": component.contribution,
        "missing": component.missing,
        "source_refs": list(component.source_refs),
    }


def _statement_payload(statement: FactStatement) -> dict:
    return {
        "text": statement.text,
        "fact_refs": list(statement.fact_refs),
        "comparison": (
            dataclasses.asdict(statement.comparison) if statement.comparison else None
        ),
    }
