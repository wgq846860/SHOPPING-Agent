"""Public Demo implementation."""
from __future__ import annotations

from dataclasses import dataclass
import math

from backend.ranking.product_ranker import (
    PRICE_DENOMINATOR_DEFAULT,
    IntentDistortion,
    ProductScore,
    _check_levers,
    _known,
    _number,
    score_product,
)
from backend.schemas.agent import PREFERENCE_DIMENSIONS, ShoppingIntent
from backend.services.candidate_service import Candidate
from backend.services.derive import OfferPlan

OFFER_ALGORITHM_VERSION = "offer-ranking.v1"
OFFER_DIMENSIONS = ("price", "shipping", "merchant_service", "returns", "warranty")
OFFER_BALANCED = {
    "price": .35, "shipping": .20, "merchant_service": .15, "returns": .15, "warranty": .15,
}
DELIVERY_DAYS_LIMIT = 7
RETURN_DAYS_LIMIT = 30
WARRANTY_MONTHS_LIMIT = 24

#: - service_first：演示正常模式，取 Offer 服务分最高方案并按该方案重算商品分；


#:   （score_product 曝光分）最高者，曝光分完全并列统一按 rank_products 口径
#:   决胜：到手价→offer_id→mode_id（删除实验分支的 Offer 服务分决胜）。
SELECTION_POLICIES = ("service_first", "lever_score")


@dataclass(frozen=True)
class OfferScoreComponent:
    dimension: str
    raw_value: object
    unit: str | None
    basis: str | None
    normalized_score: float
    weight: float
    contribution: float
    missing: bool
    source_refs: list[str]


@dataclass
class OfferScore:
    product_id: str
    offer_id: str
    mode_id: str
    merchant_id: str
    plan: OfferPlan
    score: float
    components: list[OfferScoreComponent]
    weights: dict[str, float]
    landed: int
    
    commission_rate: float | None
    commission_missing: bool
    algorithm_version: str
    warnings: list[dict]


@dataclass
class Recommendation:
    """One product's final purchase plan: recomputed product score, all offer
    scores ordered by service fit, and the recommended offer score."""

    product_score: ProductScore
    offer_scores: list[OfferScore]
    recommended: OfferScore
    
    
    # — including the 00 baseline cell — steers which merchant gets the exposure.
    platform_steered: bool


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _offer_weights(intent: ShoppingIntent) -> tuple[dict[str, float], list[dict]]:
    prefs = intent.preferences.model_dump()
    requested = {key: prefs[key] for key in OFFER_DIMENSIONS if prefs[key] > 0}
    warnings = []
    if not requested:
        requested = OFFER_BALANCED
        warnings.append({"code": "DEFAULT_WEIGHTS", "field": "preferences",
                         "message": "未表达Offer服务维度，使用offer balanced预设。"})
    total = math.fsum(requested.values())
    return {key: requested.get(key, 0.0) / total for key in PREFERENCE_DIMENSIONS}, warnings


def _source_known(source: object) -> bool:
    """Simulated field-level evidence for a derived policy leaf (dict or Source)."""
    if hasattr(source, "model_dump"):
        source = source.model_dump()
    if not isinstance(source, dict) or source.get("missing_reason") is not None:
        return False
    if not all(source.get(key) for key in ("source_type", "source_ref", "observed_at", "method")):
        return False
    return source.get("source_type") == "simulated"


def _validate_plan(plan: OfferPlan) -> None:
    if not plan.eligible or plan.reasons or plan.landed is None:
        raise ValueError(f"{plan.offer.get('offer_id')}: 需要S2.1合格且可定价的方案")
    _number(plan.landed, "landed_price_cents")
    if plan.offer.get("merchant_id") != plan.merchant.get("merchant_id"):
        raise ValueError("评分方案商家引用不一致")


def _verify_final(plan: OfferPlan, intent: ShoppingIntent) -> None:
    """Final-offer re-check (plan S2.3): never recommend an out-of-stock,
    over-budget or wrong-region offer; callers must pass S2.1 output."""
    _validate_plan(plan)
    offer = plan.offer
    stock = offer.get("stock")
    if stock is None or stock <= 0:
        raise ValueError(f"{offer['offer_id']}: 最终推荐报价必须有货")
    budget = intent.budget.max_cents
    if budget is not None and plan.landed > budget:
        raise ValueError(f"{offer['offer_id']}: 最终推荐报价超预算（{plan.landed}>{budget}分）")
    requirement = intent.hard_constraints.delivery_requirement
    region = requirement.destination_region if requirement is not None else None
    if region is not None and offer["delivery"].get("destination_region") != region:
        raise ValueError(f"{offer['offer_id']}: 最终推荐报价配送地区不满足要求")


def _feature(plan: OfferPlan, dimension: str, scale_cents: int) -> tuple[object, float, str, str, list[str], bool]:
    """Return (raw, normalized, unit, basis, refs, missing); raw None = unknown."""
    offer, merchant = plan.offer, plan.merchant
    offer_id = offer["offer_id"]
    if dimension == "price":
        return plan.landed, _clamp01(1 - plan.landed / scale_cents), "CNY_cent", "landed_price", \
            [f"{offer_id}.landed_price_cents"], False
    if dimension == "shipping":
        refs = [f"{offer_id}.delivery.min_days"]
        days = offer.get("delivery", {}).get("min_days")
        if days is None or not _known(offer, ["delivery.min_days"], simulated=True):
            return None, .5, "day", "delivery_min_days", refs, True
        return days, _clamp01(1 - days / DELIVERY_DAYS_LIMIT), "day", "delivery_min_days", refs, False
    if dimension == "merchant_service":
        refs = [f"{merchant['merchant_id']}.service_score"]
        score = merchant.get("service_score")
        if score is None or not _known(merchant, ["service_score"], simulated=True):
            return None, .5, "score_100", "simulated", refs, True
        return score, _number(score, "service_score", high=100) / 100, "score_100", "simulated", refs, False
    policies = plan.policies
    if dimension == "returns":
        # S23-R1（ACCEPT-S2.3）：days 与 opened 两叶子均须已知且各有 simulated 来源；
        # 开封政策未知/无来源时不得按最佳售后计分，整维按缺失中性处理。
        refs = [f"{offer_id}.effective_return_policy.days",
                f"{offer_id}.effective_return_policy.opened_return_allowed"]
        policy = policies.return_policy
        usable = (
            policy.days is not None
            and _source_known(policies.field_sources.get("effective_return_policy.days"))
            and isinstance(policy.opened_return_allowed, bool)
            and _source_known(policies.field_sources.get("effective_return_policy.opened_return_allowed"))
        )
        if not usable:
            return None, .5, "day", policies.policy_origin, refs, True
        normalized = _clamp01(_number(policy.days, "return_policy.days") / RETURN_DAYS_LIMIT)
        if policy.opened_return_allowed is False:
            normalized *= 0.5
        return policy.days, normalized, "day", policies.policy_origin, refs, False
    refs = [f"{offer_id}.effective_warranty.months"]
    months = policies.warranty.months
    if months is None or not _source_known(policies.field_sources.get("effective_warranty.months")):
        return None, .5, "month", policies.warranty_origin, refs, True
    return months, _clamp01(_number(months, "warranty.months") / WARRANTY_MONTHS_LIMIT), "month", \
        policies.warranty_origin, refs, False


def rank_offers(plans: list[OfferPlan], intent: ShoppingIntent) -> list[OfferScore]:
    """Rank one product's eligible plans by offer service fit (offer-ranking.v1).

    Stable order key (-score, landed, offer_id, mode_id); commission is recorded
    but never scored.
    """
    if not plans:
        return []
    product_ids = {plan.offer.get("product_id") for plan in plans}
    if None in product_ids or len(product_ids) > 1:
        raise ValueError("rank_offers 只接受同一商品的方案")
    product_id = plans[0].offer["product_id"]
    weights, base_warnings = _offer_weights(intent)
    scale_cents = intent.budget.max_cents or PRICE_DENOMINATOR_DEFAULT
    result = []
    for plan in plans:
        _validate_plan(plan)
        warnings = [dict(warning) for warning in base_warnings]
        components = []
        for dimension in PREFERENCE_DIMENSIONS:
            weight = weights[dimension]
            if weight == 0:
                continue
            raw, normalized, unit, basis, refs, missing = _feature(plan, dimension, scale_cents)
            components.append(OfferScoreComponent(dimension, raw, unit, basis, normalized, weight,
                                                  normalized * weight * 100, missing, refs))
            if missing:
                warnings.append({"code": "MISSING_SOFT_METRIC", "product_id": product_id,
                                 "field": dimension,
                                 "message": f"报价{plan.offer['offer_id']}的Offer服务软指标未知，"
                                            f"使用固定中性0.5；未更改原始事实。"})
        commission = plan.offer.get("commission_rate")
        if commission is not None:
            _number(commission, "commission_rate", high=1)
        if commission is None or not _known(plan.offer, ["commission_rate"]):
            commission = None
        offer_id = plan.offer["offer_id"]
        result.append(OfferScore(product_id, offer_id, plan.mode_id, plan.merchant["merchant_id"], plan,
                                 math.fsum(component.contribution for component in components),
                                 components, weights, plan.landed, commission, commission is None,
                                 OFFER_ALGORITHM_VERSION, warnings))
    return sorted(result, key=lambda s: (-s.score, s.plan.landed, s.offer_id, s.mode_id))


def recommend(candidates: list[Candidate], intent: ShoppingIntent, *, platform_bias: float = 0.0,
              intent_distortion: IntentDistortion | None = None,
              bias_form: str = "headroom",
              selection_policy: str = "service_first") -> list[Recommendation]:
    """Public Demo implementation."""
    _check_levers(platform_bias, intent_distortion, bias_form)
    if selection_policy not in SELECTION_POLICIES:
        raise ValueError(f"selection_policy: 须为{SELECTION_POLICIES}之一")
    active_levers = platform_bias > 0 or (
        intent_distortion is not None and intent_distortion.strength > 0
    )
    if selection_policy == "service_first" and active_levers:
        raise ValueError(
            "service_first 为演示正常模式，不接受非零扩展杠杆；"
            "实验请显式使用 selection_policy='lever_score'")
    results: list[Recommendation] = []
    for candidate in candidates:
        if not candidate.eligible_plans:
            raise ValueError(f"{candidate.product_id}: 缺少合格报价方案")
        offer_scores = rank_offers(candidate.eligible_plans, intent)
        if selection_policy == "lever_score":
            plan_scores = [score_product(candidate, intent, plan, platform_bias,
                                         intent_distortion=intent_distortion, bias_form=bias_form)
                           for plan in candidate.eligible_plans]
            top = max(score.score for score in plan_scores)
            tied = [score for score in plan_scores if score.score == top]
            chosen = min(tied, key=lambda s: (s.selected_plan.landed,
                                              s.selected_plan.offer["offer_id"],
                                              s.selected_plan.mode_id))
            _verify_final(chosen.selected_plan, intent)
            chosen_offer = next(
                score for score in offer_scores
                if score.offer_id == chosen.selected_plan.offer["offer_id"]
                and score.mode_id == chosen.selected_plan.mode_id
            )
            results.append(Recommendation(chosen, offer_scores, chosen_offer, True))
        else:
            best = offer_scores[0]
            _verify_final(best.plan, intent)
            results.append(Recommendation(score_product(candidate, intent, best.plan),
                                          offer_scores, best, False))
    return sorted(results, key=lambda r: (-r.product_score.score, r.product_score.product_id))
