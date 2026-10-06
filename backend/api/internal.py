"""Public Demo implementation."""

from __future__ import annotations

import dataclasses
import hmac
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.config import DEFAULT_DESTINATION_REGION
from backend.agent.decision_records import DecisionBindingError
from backend.ranking.offer_ranker import recommend
from backend.schemas.agent import ShoppingIntent
from backend.services import derive, product_service
from backend.services.candidate_service import get_eligible_candidates
from backend.services.errors import DataError, InvalidMode

#: 送入模型上下文的候选上限（S3.2 Formatter 取 10–20，工具层先截 10）
BRIDGE_MAX_CANDIDATES = 10

#: 简化内部错误信封（与对外 api.v0.1 信封区分）
TOKEN_HEADER = "x-agent-bridge-token"

router = APIRouter(prefix="/api/internal/tools", tags=["agent-internal"])


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecommendRequest(_StrictModel):
    """recommend_from_intent 入参：受校验的 ShoppingIntent 本体。"""

    intent: ShoppingIntent
    decision_request_id: str | None = Field(None, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")


class ProductDetailRequest(_StrictModel):
    product_id: str = Field(min_length=1, max_length=64)


class ProductOffersRequest(_StrictModel):
    product_id: str = Field(min_length=1, max_length=64)
    mode_id: str | None = Field(None, min_length=1, max_length=64)
    destination_region: Literal["CN-SH"] = DEFAULT_DESTINATION_REGION


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _now(request: Request) -> datetime:
    """参考时间：测试经 app.state.clock 注入（与 backend/api/products.py 一致）。"""
    clock = getattr(request.app.state, "clock", None)
    if callable(clock):
        return clock()
    return datetime.now(derive.CN_TZ)


def _guard(request: Request) -> JSONResponse | None:
    """令牌校验：未配置即桥关闭（503）；令牌不符（403）。常量时间比较。"""
    settings = request.app.state.settings
    expected = settings.agent_bridge_token()
    if expected is None:
        return _error(503, "bridge_disabled", "工具桥未启用（未配置 AGENT_BRIDGE_TOKEN）")
    provided = request.headers.get(TOKEN_HEADER, "")
    if not hmac.compare_digest(expected.encode(), provided.encode()):
        return _error(403, "bridge_forbidden", "工具桥令牌无效")
    return None


def _open_db(request: Request):
    from backend.database.db import connect

    settings = request.app.state.settings
    return connect(settings.db_path)


def _data_version(conn) -> str | None:
    from backend.database.db import get_manifest

    manifest = get_manifest(conn)
    return manifest.get("data_version") if manifest else None


def _serialize_filters(records) -> list[dict]:
    return [dataclasses.asdict(record) for record in records]


def _intent_json(intent: ShoppingIntent) -> dict:
    """JSON 模式序列化：date 等类型转 ISO 字符串（S31-05，三个分支共用）。"""
    return intent.model_dump(mode="json")


def _offer_plan_counts(candidates) -> tuple[int, int]:
    """Public Demo implementation."""
    offer_ids = {
        plan.offer["offer_id"]
        for candidate in candidates
        for plan in candidate.eligible_plans
    }
    plan_count = sum(len(candidate.eligible_plans) for candidate in candidates)
    return len(offer_ids), plan_count


def _recommendation_item(rank: int, rec: dict) -> dict:
    """压缩为模型上下文所需的事实字段；分数保留 6 位小数供重算。"""
    product = rec["product_score"]
    plan = rec["recommended"].plan
    offer = plan.offer
    merchant = plan.merchant
    return {
        "rank": rank,
        "product_id": product.product_id,
        "product_name": product.candidate.product.get("product_name"),
        "brand": product.candidate.product.get("brand"),
        "sub_category": product.candidate.product.get("sub_category"),
        "offer_id": offer["offer_id"],
        "merchant_id": merchant["merchant_id"],
        "merchant_name": merchant["merchant_name"],
        "mode_id": rec["recommended"].mode_id,
        "landed_price_cents": plan.landed,
        "price_cents": offer.get("price_cents"),
        "shipping_fee_cents": offer.get("shipping_fee_cents"),
        "accessory_total_cents": plan.accessory_total,
        "instant_discount_cents": offer.get("instant_discount_cents"),
        "stock": offer.get("stock"),
        "product_score": round(product.score, 6),
        "product_base_score": round(product.base_score, 6),
        "offer_score": round(rec["recommended"].score, 6),
        "selected_specs": product.selected_specs,
        "platform_steered": rec["platform_steered"],
    }


@router.post("/recommend_from_intent")
def recommend_from_intent(payload: RecommendRequest, request: Request) -> JSONResponse:
    """组合工具：意图 → 检索/硬过滤 → 商品/Offer 排序 → 决策记录。

    候选为空、需要澄清都如实返回，不放宽条件；本接口不生成自然语言
    解释（S3.2 另行接入 Formatter）。
    """
    denied = _guard(request)
    if denied is not None:
        return denied
    intent = payload.intent
    conn = _open_db(request)
    try:
        if payload.decision_request_id is not None:
            try:
                prepared = request.app.state.decision_records.prepare(payload.decision_request_id, intent, conn)
            except DecisionBindingError:
                return _error(409, "decision_binding_invalid", "工具请求与当前购物决策不匹配或已结束")
            top = prepared.recommendations[:BRIDGE_MAX_CANDIDATES]
            record = {
                "decision_request_id": payload.decision_request_id,
                "status": prepared.body["status"], "intent": _intent_json(prepared.intent),
                "recommendations": [
                    _recommendation_item(rank, {"product_score": rec.product_score,
                                                "recommended": rec.recommended,
                                                "platform_steered": rec.platform_steered})
                    for rank, rec in enumerate(top, start=1)
                ],
                "decision_summary": {**prepared.body["decision_summary"], "returned_count": len(top)},
                "filters_applied": prepared.body["filters_applied"],
                "ranking_weights": prepared.body["ranking_weights"],
                "warnings": prepared.body["warnings"],
                "algorithm_version": prepared.body["algorithm_version"],
                "selection_policy": "service_first", "data_version": prepared.data_version,
            }
            return JSONResponse(record)
        now = _now(request)
        data_version = _data_version(conn)
        search = get_eligible_candidates(conn, intent, now=now)

        if search.status == "needs_clarification":
            return JSONResponse(
                {
                    "status": "needs_clarification",
                    "intent": _intent_json(intent),
                    "recommendations": [],
                    "clarifications": [c.model_dump() for c in search.clarifications],
                    "decision_summary": {
                        "candidate_count": 0,
                        "eligible_offer_count": 0,
                        "returned_count": 0,
                        "message": "需求存在冲突或缺少必要信息，先澄清再推荐",
                    },
                    "filters_applied": [],
                    "ranking_weights": {"product": {}, "offer": {}},
                    "warnings": search.warnings,
                    "algorithm_version": None,
                    "selection_policy": "service_first",
                    "data_version": data_version,
                }
            )

        if not search.candidates:
            return JSONResponse(
                {
                    "status": "no_results",
                    "intent": _intent_json(intent),
                    "recommendations": [],
                    "clarifications": [],
                    "decision_summary": {
                        "candidate_count": 0,
                        "eligible_offer_count": 0,
                        "returned_count": 0,
                        "message": "没有满足全部硬条件的商品报价方案",
                    },
                    "filters_applied": _serialize_filters(search.filters_applied),
                    "ranking_weights": {"product": {}, "offer": {}},
                    "warnings": search.warnings,
                    "algorithm_version": None,
                    "selection_policy": "service_first",
                    "data_version": data_version,
                }
            )

        recs = recommend(search.candidates, intent, selection_policy="service_first")
        top = recs[:BRIDGE_MAX_CANDIDATES]
        eligible_offer_count, eligible_plan_count = _offer_plan_counts(search.candidates)
        record = {
            "status": "ok" if len(top) >= 3 else "partial",
            "intent": _intent_json(intent),
            "recommendations": [
                _recommendation_item(rank, {"product_score": rec.product_score,
                                             "recommended": rec.recommended,
                                             "platform_steered": rec.platform_steered})
                for rank, rec in enumerate(top, start=1)
            ],
            "clarifications": [],
            "decision_summary": {
                "candidate_count": len(search.candidates),
                "eligible_offer_count": eligible_offer_count,
                "eligible_plan_count": eligible_plan_count,
                "returned_count": len(top),
                "message": f"硬约束后共 {len(search.candidates)} 个商品、"
                f"{eligible_offer_count} 条合格报价（{eligible_plan_count} 个方案），"
                f"返回前 {len(top)} 个",
            },
            "filters_applied": _serialize_filters(search.filters_applied),
            "ranking_weights": {
                "product": top[0].product_score.weights,
                "offer": top[0].recommended.weights,
            },
            "warnings": search.warnings,
            "algorithm_version": top[0].product_score.algorithm_version,
            "offer_algorithm_version": top[0].recommended.algorithm_version,
            "selection_policy": "service_first",
            "data_version": data_version,
        }
        return JSONResponse(record)
    finally:
        conn.close()


@router.post("/get_product_details")
def get_product_details(payload: ProductDetailRequest, request: Request) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    conn = _open_db(request)
    try:
        detail = product_service.get_product_detail(conn, payload.product_id)
        if detail is None:
            return _error(404, "product_not_found", f"商品 {payload.product_id} 不存在")
        return JSONResponse(
            {"product": detail["product"], "metrics": detail["metrics"],
             "data_version": _data_version(conn)}
        )
    finally:
        conn.close()


@router.post("/get_product_offers")
def get_product_offers(payload: ProductOffersRequest, request: Request) -> JSONResponse:
    denied = _guard(request)
    if denied is not None:
        return denied
    conn = _open_db(request)
    try:
        result = product_service.get_product_offers(
            conn,
            payload.product_id,
            region=payload.destination_region,
            mode_id=payload.mode_id,
            now=_now(request),
        )
        if result is None:
            return _error(404, "product_not_found", f"商品 {payload.product_id} 不存在")
        result["data_version"] = _data_version(conn)
        return JSONResponse(result)
    except InvalidMode:
        return _error(422, "invalid_mode", "mode_id 不属于该商品")
    finally:
        conn.close()
