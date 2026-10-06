"""S2.3 acceptance tests for the documented offer-ranking.v1 baseline.

Fixtures reuse the S2.2 helpers (real Candidate / derive.build_plan objects,
no database). Expected scores are small, independently calculable examples
against the frozen rules in docs/ranking_baseline.md.
"""
from __future__ import annotations

import math

import pytest

from backend.ranking.offer_ranker import (
    OFFER_ALGORITHM_VERSION,
    OFFER_BALANCED,
    Recommendation,
    rank_offers,
    recommend,
)
from backend.ranking.product_ranker import IntentDistortion, rank_products, score_product
from backend.schemas.agent import PREFERENCE_DIMENSIONS
from backend.services.derive import OfferPlan, build_plan
from backend.services.candidate_service import Candidate
from backend.schemas.common import POLICY_ORIGIN_MERCHANT, POLICY_ORIGIN_OFFER
from tests.test_ranking import NOW, _intent, _metrics, _offer, _product, _source

RETURN_LEAVES = ("days", "opened_return_allowed", "return_shipping_fee_cents", "conditions", "exceptions")
WARRANTY_LEAVES = ("months", "provider", "conditions")


def _merchant_full(merchant_id="M001", *, service=90, return_days=30, opened=True, warranty=12):
    merchant = {
        "merchant_id": merchant_id,
        "service_score": service,
        "default_return_policy": {"days": return_days, "opened_return_allowed": opened,
                                  "return_shipping_fee_cents": 0, "conditions": "reference policy",
                                  "exceptions": []},
        "default_warranty": {"months": warranty, "provider": merchant_id, "conditions": "reference warranty"},
    }
    sources = {"service_score": _source(f"{merchant_id}.service_score")}
    for leaf in RETURN_LEAVES:
        sources[f"default_return_policy.{leaf}"] = _source(f"{merchant_id}.default_return_policy.{leaf}")
    for leaf in WARRANTY_LEAVES:
        sources[f"default_warranty.{leaf}"] = _source(f"{merchant_id}.default_warranty.{leaf}")
    merchant["field_sources"] = sources
    return merchant


def _plan(product, offer, merchant, *, mode_id="bt", region="CN-SH"):
    return build_plan(product, offer, merchant, mode_id=mode_id, region=region, now=NOW)


def _cand(product_id="P001", *, metrics=None, offers=None, merchants=None, product=None):
    """Candidate with per-merchant dicts; every plan must be S2.1 eligible."""
    product = product if product is not None else _product(product_id)
    metrics = metrics if metrics is not None else _metrics(product["product_id"])
    merchants = merchants or {}
    plans = []
    for offer in offers:
        merchant = merchants.get(offer["merchant_id"], _merchant_full(offer["merchant_id"]))
        plan = _plan(product, offer, merchant)
        assert plan.eligible, f"fixture plan {offer['offer_id']} must be eligible: {plan.reasons}"
        plans.append(plan)
    return Candidate(product=product, metrics=metrics, eligible_plans=plans)


def _override_offer(offer_id="O001", product_id="P001", *, price=50000, merchant_id="M001",
                    return_override=None, warranty_override=None, min_days=1, commission=None):
    offer = _offer(offer_id, product_id, price=price, merchant_id=merchant_id, commission=commission)
    offer["delivery"]["min_days"] = min_days
    if return_override is not None:
        offer["return_policy_override"] = return_override
        for leaf in RETURN_LEAVES:
            offer["field_sources"][f"return_policy_override.{leaf}"] = _source(f"{offer_id}.return_policy_override.{leaf}")
    if warranty_override is not None:
        offer["warranty_override"] = warranty_override
        for leaf in WARRANTY_LEAVES:
            offer["field_sources"][f"warranty_override.{leaf}"] = _source(f"{offer_id}.warranty_override.{leaf}")
    return offer


def _component(score, dimension):
    return next(c for c in score.components if c.dimension == dimension)


def _codes(score):
    return {w["code"] for w in score.warnings}


# ---------------------------------------------------------------------------
# 权重与结构
# ---------------------------------------------------------------------------

def test_offer_balanced_preset_when_no_offer_preferences():
    offer = _override_offer()
    intent = _intent(budget=100000)  # 未表达任何Offer维度 -> 预设
    ranked = rank_offers([_plan(_product(), offer, _merchant_full())], intent)
    assert len(ranked) == 1
    result = ranked[0]
    assert set(result.weights) == set(PREFERENCE_DIMENSIONS)
    for key, weight in OFFER_BALANCED.items():
        assert result.weights[key] == pytest.approx(weight)
    assert math.fsum(result.weights.values()) == pytest.approx(1)
    assert "DEFAULT_WEIGHTS" in _codes(result)
    assert result.algorithm_version == OFFER_ALGORITHM_VERSION == "offer-ranking.v1"
    assert result.commission_missing and result.commission_rate is None


def test_explicit_offer_preferences_replace_preset():
    offer = _override_offer()
    intent = _intent(preferences={"shipping": .8, "warranty": .2}, budget=100000)
    result = rank_offers([_plan(_product(), offer, _merchant_full())], intent)[0]
    assert result.weights["shipping"] == pytest.approx(.8)
    assert result.weights["warranty"] == pytest.approx(.2)
    assert result.weights["price"] == 0
    assert [c.dimension for c in result.components] == ["shipping", "warranty"]
    assert "DEFAULT_WEIGHTS" not in _codes(result)


def test_product_preferences_do_not_enter_the_offer_group():
    offer = _override_offer()
    intent = _intent(preferences={"comfort": 1}, budget=100000)  # 商品维偏好不带入Offer组
    result = rank_offers([_plan(_product(), offer, _merchant_full())], intent)[0]
    for key, weight in OFFER_BALANCED.items():
        assert result.weights[key] == pytest.approx(weight)
    assert "UNSUPPORTED_PREFERENCE" not in _codes(result)  # 商品维告警属商品排序，不在此重复


# ---------------------------------------------------------------------------
# 手算计分
# ---------------------------------------------------------------------------

def test_offer_components_are_hand_calculable():
    # landed 50000/预算100000 -> price .5；min_days 2 -> 1-2/7；service 90 -> .9；
    # 退货30天可开封 -> 1.0；保修24月 -> 1.0；balanced 权重合计应得 75.285714…
    offer = _override_offer(min_days=2)
    merchant = _merchant_full(service=90, return_days=30, opened=True, warranty=12 + 12)
    result = rank_offers([_plan(_product(), offer, merchant)], _intent(budget=100000))[0]
    price = _component(result, "price")
    assert (price.raw_value, price.normalized_score, price.unit) == (50000, pytest.approx(.5), "CNY_cent")
    shipping = _component(result, "shipping")
    assert (shipping.raw_value, shipping.normalized_score) == (2, pytest.approx(1 - 2 / 7))
    service = _component(result, "merchant_service")
    assert (service.raw_value, service.normalized_score) == (90, pytest.approx(.9))
    returns = _component(result, "returns")
    assert (returns.raw_value, returns.normalized_score) == (30, pytest.approx(1.0))
    warranty = _component(result, "warranty")
    assert (warranty.raw_value, warranty.normalized_score) == (24, pytest.approx(1.0))
    expected = 100 * (.35 * .5 + .20 * (1 - 2 / 7) + .15 * .9 + .15 * 1.0 + .15 * 1.0)
    assert result.score == pytest.approx(expected)  # 75.285714…
    assert math.fsum(c.contribution for c in result.components) == pytest.approx(result.score)


def test_opened_return_penalty_halves_the_returns_score():
    opened_ok = _plan(_product(), _override_offer(), _merchant_full(opened=True))
    no_opened = _plan(_product(), _override_offer("O002", merchant_id="M002"), _merchant_full("M002", opened=False))
    intent = _intent(preferences={"returns": 1}, budget=100000)
    ok, bad = rank_offers([opened_ok, no_opened], intent)
    assert _component(ok, "returns").normalized_score == pytest.approx(1.0)
    assert _component(bad, "returns").normalized_score == pytest.approx(.5)
    assert ok.score > bad.score


def test_unknown_after_sales_is_neutral_not_best():
    # 退货天数未知 / 保修月数未知 -> raw null、0.5 中性、MISSING_SOFT_METRIC，不按满分计
    offer = _override_offer(return_override={"days": None, "opened_return_allowed": True,
                                             "return_shipping_fee_cents": 0, "conditions": None, "exceptions": []},
                            warranty_override={"months": None, "provider": None, "conditions": None})
    intent = _intent(preferences={"returns": .5, "warranty": .5}, budget=100000)
    result = rank_offers([_plan(_product(), offer, _merchant_full())], intent)[0]
    assert _component(result, "returns").missing and _component(result, "returns").normalized_score == .5
    assert _component(result, "warranty").missing and _component(result, "warranty").normalized_score == .5
    assert result.score == pytest.approx(50)
    assert "MISSING_SOFT_METRIC" in _codes(result)


def test_unknown_delivery_service_and_missing_source_are_neutral():
    offer = _override_offer(min_days=1)
    offer["delivery"]["min_days"] = None  # 最早送达日未知
    merchant = _merchant_full()
    del merchant["service_score"]  # 商家服务分缺失
    merchant["field_sources"].pop("service_score")
    intent = _intent(preferences={"shipping": .5, "merchant_service": .5}, budget=100000)
    result = rank_offers([_plan(_product(), offer, merchant)], intent)[0]
    assert _component(result, "shipping").missing and _component(result, "shipping").normalized_score == .5
    assert _component(result, "merchant_service").missing
    assert result.score == pytest.approx(50)


def test_policy_merge_prefers_offer_override_entirely():
    # 商家默认 30天可开封/12月保修；Offer 覆盖 7天不开封/24月保修 -> 有效值=覆盖对象整体
    offer = _override_offer(return_override={"days": 7, "opened_return_allowed": False,
                                             "return_shipping_fee_cents": 1200, "conditions": "仅未开封",
                                             "exceptions": []},
                            warranty_override={"months": 24, "provider": "AudioWorld", "conditions": "模拟"})
    merchant = _merchant_full(return_days=30, opened=True, warranty=12)
    plan = _plan(_product(), offer, merchant)
    assert plan.policies.policy_origin == POLICY_ORIGIN_OFFER == "offer_override"
    assert plan.policies.warranty_origin == POLICY_ORIGIN_OFFER
    intent = _intent(preferences={"returns": .5, "warranty": .5}, budget=100000)
    result = rank_offers([plan], intent)[0]
    assert _component(result, "returns").normalized_score == pytest.approx(7 / 30 * .5)
    assert _component(result, "returns").basis == POLICY_ORIGIN_OFFER
    assert _component(result, "warranty").normalized_score == pytest.approx(1.0)
    # 无覆盖时走商家默认
    plain = rank_offers([_plan(_product(), _override_offer(), _merchant_full(return_days=30, warranty=12))],
                        intent)[0]
    assert _component(plain, "returns").basis == POLICY_ORIGIN_MERCHANT == "merchant_default"
    assert _component(plain, "returns").normalized_score == pytest.approx(1.0)
    assert _component(plain, "warranty").normalized_score == pytest.approx(.5)


# ---------------------------------------------------------------------------
# 排序与稳定性（验收：最便宜不一定第一 / 同价稳定）
# ---------------------------------------------------------------------------

def test_cheapest_offer_is_not_necessarily_first():
    # O-cheap 42000分：service50/8天/7天不可开封退货/3月保修；O-premium 50000分：95/1天/30天可开封/24月
    cheap = _override_offer("O010", price=42000, merchant_id="M003", min_days=8)
    premium = _override_offer("O011", price=50000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50, return_days=7, opened=False, warranty=3),
                 "M001": _merchant_full("M001", service=95, return_days=30, opened=True, warranty=24)}
    candidate = _cand(offers=[cheap, premium], merchants=merchants)
    ranked = rank_offers(candidate.eligible_plans, _intent(budget=100000))
    assert [s.offer_id for s in ranked] == ["O011", "O010"]
    cheap_score, premium_score = ranked[1], ranked[0]
    assert _component(premium_score, "price").contribution < _component(cheap_score, "price").contribution
    assert cheap_score.score == pytest.approx(
        100 * (.35 * (1 - .42) + .20 * 0 + .15 * .5 + .15 * (7 / 30 * .5) + .15 * (3 / 24)))
    assert premium_score.score == pytest.approx(
        100 * (.35 * .5 + .20 * 6 / 7 + .15 * .95 + .15 * 1 + .15 * 1))
    assert premium_score.score > cheap_score.score  # 更贵但服务/配送/售后显著更好


def test_same_score_ties_are_deterministic_and_stable():
    offers = [_override_offer("O002", merchant_id="M001"), _override_offer("O003", merchant_id="M002")]
    merchants = {"M001": _merchant_full("M001"), "M002": _merchant_full("M002")}
    candidate = _cand(offers=offers, merchants=merchants)
    intent = _intent(budget=100000)
    first = rank_offers(candidate.eligible_plans, intent)
    assert [s.offer_id for s in first] == ["O002", "O003"]
    assert first[0].score == first[1].score
    assert [s.offer_id for s in rank_offers(list(reversed(candidate.eligible_plans)), intent)] == ["O002", "O003"]
    assert [s.offer_id for s in rank_offers(candidate.eligible_plans, intent)] == [s.offer_id for s in first]


def test_same_offer_multiple_modes_breaks_by_mode_id():
    product = _product(modes=[
        {"mode_id": "dongle", "transport": "wireless_dongle", "playback_supported": True,
         "active_noise_cancelling": None, "microphone_supported": None, "requires_power": True,
         "required_accessory_codes": []},
        {"mode_id": "bt", "transport": "bluetooth", "playback_supported": True,
         "active_noise_cancelling": True, "microphone_supported": True, "requires_power": True,
         "required_accessory_codes": []},
    ])
    for index in range(len(product["connection_modes"])):
        product["field_sources"].update(
            {f"connection_modes[{index}].{key}": _source(f"P001.m{index}.{key}")
             for key in ("transport", "playback_supported", "active_noise_cancelling",
                         "microphone_supported", "requires_power")})
    offer = _override_offer()
    merchant = _merchant_full()
    plans = [_plan(product, offer, merchant, mode_id="bt"), _plan(product, offer, merchant, mode_id="dongle")]
    ranked = rank_offers(plans, _intent(budget=100000))
    assert [(s.offer_id, s.mode_id) for s in ranked] == [("O001", "bt"), ("O001", "dongle")]
    assert ranked[0].score == ranked[1].score


def test_commission_is_recorded_but_never_scored():
    low = _override_offer("O002", commission=.05)
    high = _override_offer("O001", commission=.3)
    candidate = _cand(offers=[low, high])
    intent = _intent(budget=100000)
    ranked = rank_offers(candidate.eligible_plans, intent)
    assert ranked[0].score == ranked[1].score  # 佣金不改变Offer得分
    assert [s.offer_id for s in ranked] == ["O001", "O002"]  # 平手按 offer_id
    assert ranked[0].commission_rate == .3 and not ranked[0].commission_missing
    assert ranked[1].commission_rate == .05


def test_cross_product_or_ineligible_plans_are_rejected():
    p1, p2 = _product("P001"), _product("P002")
    plan1 = _plan(p1, _override_offer("O001", "P001"), _merchant_full())
    plan2 = _plan(p2, _override_offer("O002", "P002"), _merchant_full())
    with pytest.raises(ValueError):
        rank_offers([plan1, plan2], _intent(budget=100000))
    expired = _override_offer()
    expired["valid_until"] = "2026-09-29T00:00:00+08:00"  # 早于 NOW
    stale = _plan(p1, expired, _merchant_full())
    assert not stale.eligible
    with pytest.raises(ValueError):
        rank_offers([stale], _intent(budget=100000))


# ---------------------------------------------------------------------------
# 最终方案组合：正常模式
# ---------------------------------------------------------------------------

def test_normal_mode_recommends_best_service_and_rescores_product():
    cheap = _override_offer("O010", price=30000, merchant_id="M003", min_days=8)
    premium = _override_offer("O011", price=80000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50, return_days=7, opened=False, warranty=3),
                 "M001": _merchant_full("M001", service=95, return_days=30, opened=True, warranty=24)}
    candidate = _cand(metrics=_metrics("P001", comfort_score=90), offers=[cheap, premium], merchants=merchants)
    # 商品组 price/comfort；Offer组只看商家服务 -> 推荐 O011（贵但服务好）
    intent = _intent(preferences={"price": .6, "comfort": .4, "merchant_service": 1}, budget=100000)
    result = recommend([candidate], intent)[0]
    assert isinstance(result, Recommendation)
    assert result.platform_steered is False
    assert result.recommended.offer_id == "O011"
    assert result.offer_scores[0].offer_id == "O011"
    # 商品价格分与最终推荐 Offer 一致：用 O011 的 80000 分重算
    assert result.product_score.selected_plan is result.recommended.plan
    assert _price_raw(result.product_score) == 80000
    assert result.product_score.score == pytest.approx(100 * (.6 * .2 + .4 * .9))
    # 两组权重分别输出、各自合计 1（Offer组内 price .6 与 merchant_service 1 归一化）
    assert result.product_score.weights["price"] == pytest.approx(.6)
    assert result.recommended.weights["merchant_service"] == pytest.approx(1 / 1.6)
    assert result.recommended.weights["price"] == pytest.approx(.6 / 1.6)
    assert math.fsum(result.product_score.weights.values()) == pytest.approx(1)
    assert math.fsum(result.recommended.weights.values()) == pytest.approx(1)


def test_product_reranking_follows_the_final_offer_price():
    # PA 若按自身最低价方案计分(.78)应排第一；推荐 Offer 服务分选中贵方案后(.48)落到 PB(.54) 之后
    pa_cheap = _override_offer("O001", "PA", price=30000, merchant_id="M003", min_days=8)
    pa_premium = _override_offer("O002", "PA", price=80000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50, return_days=7, opened=False, warranty=3),
                 "M001": _merchant_full("M001", service=95, return_days=30, opened=True, warranty=24)}
    pa = _cand("PA", metrics=_metrics("PA", comfort_score=90), offers=[pa_cheap, pa_premium], merchants=merchants)
    pb = _cand("PB", metrics=_metrics("PB", comfort_score=30),
               offers=[_override_offer("O003", "PB", price=30000, merchant_id="M001", min_days=1)],
               merchants=merchants)
    intent = _intent(preferences={"price": .6, "comfort": .4, "merchant_service": 1}, budget=100000)
    plain = [r.product_id for r in rank_products([pa, pb], intent)]
    assert plain == ["PA", "PB"]  # 未组合前 PA 按便宜方案 0.78 领先
    composed = recommend([pa, pb], intent)
    assert [r.product_score.product_id for r in composed] == ["PB", "PA"]
    assert composed[1].product_score.score == pytest.approx(48)
    assert composed[0].product_score.score == pytest.approx(100 * (.6 * .7 + .4 * .3))


def test_normal_mode_is_repeatable():
    cheap = _override_offer("O010", price=30000, merchant_id="M003", min_days=8)
    premium = _override_offer("O011", price=80000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50, return_days=7, opened=False, warranty=3),
                 "M001": _merchant_full("M001", service=95, return_days=30, opened=True, warranty=24)}
    candidate = _cand(offers=[cheap, premium], merchants=merchants)
    intent = _intent(budget=100000)
    first = [(r.recommended.offer_id, r.product_score.score) for r in recommend([candidate], intent)]
    second = [(r.recommended.offer_id, r.product_score.score) for r in recommend([candidate], intent)]
    assert first == second


def test_final_offer_recheck_rejects_stock_budget_and_region_violations():
    candidate = _cand(offers=[_override_offer(price=50000)], merchants={"M001": _merchant_full()})
    intent = _intent(budget=60000)
    assert recommend([candidate], intent)[0].recommended.offer_id == "O001"
    mutant = _cand(offers=[_override_offer(price=50000)], merchants={"M001": _merchant_full()})
    mutant.eligible_plans[0].offer["stock"] = 0  # 构造缺货的"合格"方案（复核应拦下）
    with pytest.raises(ValueError, match="有货"):
        recommend([mutant], intent)
    over = _cand(offers=[_override_offer(price=50000)], merchants={"M001": _merchant_full()})
    over.eligible_plans[0].landed = 90000  # 构造超预算方案
    with pytest.raises(ValueError, match="超预算"):
        recommend([over], intent)
    wrong_region = _cand(offers=[_override_offer(price=50000)], merchants={"M001": _merchant_full()})
    wrong_region.eligible_plans[0].offer["delivery"]["destination_region"] = "CN-BJ"
    with pytest.raises(ValueError, match="配送地区"):
        recommend([wrong_region], _intent(budget=60000, hard=_delivery_intent()))


def _delivery_intent():
    from backend.schemas.agent import DeliveryRequirement, HardConstraints
    return HardConstraints(delivery_requirement=DeliveryRequirement(
        destination_region="CN-SH", deadline_date=None, commitment_type=None, conditions=None))


def _price_raw(product_score):
    return next(c.raw_value for c in product_score.components if c.dimension == "price")


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

def test_experiment_mode_takes_the_lever_best_offer_and_marks_steering():
    cheap = _override_offer("O010", price=50000, merchant_id="M003")
    expensive = _override_offer("O011", price=58000, merchant_id="M001", commission=.5)
    merchants = {"M003": _merchant_full("M003"), "M001": _merchant_full("M001")}
    candidate = _cand(offers=[cheap, expensive], merchants=merchants)
    intent = _intent(preferences={"price": 1}, budget=100000)
    normal = recommend([candidate], intent)[0]
    assert normal.platform_steered is False and normal.recommended.offer_id == "O010"
    steered = recommend([candidate], intent, platform_bias=1.0,
                        selection_policy="lever_score")[0]
    assert steered.platform_steered is True
    assert steered.recommended.offer_id == "O011"  # λ=1,c=.5：杠杆分 42+29=71 > 50
    assert steered.product_score.score == pytest.approx(100 * (1 - .58) + 1 * .5 * (100 - 42))
    assert steered.product_score.bias_contribution > 0
    # 服务分仅展示：被推荐报价不是服务分最高者（O010 服务分50 > O011 的42），由杠杆分决定
    assert steered.offer_scores[0].offer_id == "O010"
    assert steered.recommended.score < steered.offer_scores[0].score
    # OfferScore 里两条报价都在，供对比展示
    assert {s.offer_id for s in steered.offer_scores} == {"O010", "O011"}


def test_exact_lever_ties_break_by_price_offer_mode_in_experiment_mode():
    
    # 删除实验分支的 Offer 服务分决胜——服务更好的 O002 不再因并列胜出。
    good = _override_offer("O002", price=50000, merchant_id="M002")
    bad = _override_offer("O001", price=50000, merchant_id="M003")
    merchants = {"M002": _merchant_full("M002", service=90), "M003": _merchant_full("M003", service=60)}
    candidate = _cand(offers=[good, bad], merchants=merchants)
    intent = _intent(preferences={"price": 1, "merchant_service": 1}, budget=100000)
    distortion = IntentDistortion(.5, {"comfort": 1})
    plain = rank_products([candidate], intent, intent_distortion=distortion)[0]
    assert plain.selected_plan.offer["offer_id"] == "O001"  # landed/offer_id 决胜
    steered = recommend([candidate], intent, intent_distortion=distortion,
                        selection_policy="lever_score")[0]
    assert steered.platform_steered is True
    assert steered.recommended.offer_id == "O001"  # 与 rank_products 同口径
    assert steered.product_score.score == pytest.approx(plain.score)  # 并列组内分数相同


def test_experiment_product_order_matches_rank_products():
    pa = _cand("PA", metrics=_metrics("PA", comfort_score=90),
               offers=[_override_offer("O001", "PA", price=40000, merchant_id="M001")])
    pb = _cand("PB", metrics=_metrics("PB", comfort_score=30),
               offers=[_override_offer("O002", "PB", price=80000, merchant_id="M001")])
    intent = _intent(preferences={"comfort": 1}, budget=100000)
    distortion = IntentDistortion(.5, {"price": 1})
    expected = [r.product_id for r in rank_products([pb, pa], intent, intent_distortion=distortion)]
    assert [r.product_score.product_id for r in recommend(
        [pb, pa], intent, intent_distortion=distortion, selection_policy="lever_score")] == expected


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

def test_service_first_rejects_active_levers():
    """service_first 为演示正常模式：λ/μ 非零（含 1e-12 这类微扰）一律受控拒绝，
    不再隐式切换选择规则，也不静默忽略参数。"""
    candidate = _cand(offers=[_override_offer()], merchants={"M001": _merchant_full()})
    intent = _intent(preferences={"price": 1}, budget=100000)
    with pytest.raises(ValueError, match="lever_score"):
        recommend([candidate], intent, platform_bias=1e-12)
    with pytest.raises(ValueError, match="lever_score"):
        recommend([candidate], intent, intent_distortion=IntentDistortion(.5, {"comfort": 1}))


def test_invalid_selection_policy_is_rejected():
    candidate = _cand(offers=[_override_offer()], merchants={"M001": _merchant_full()})
    with pytest.raises(ValueError, match="selection_policy"):
        recommend([candidate], _intent(budget=100000), selection_policy="best_effort")


def _selection(candidates, intent, **levers) -> list[tuple[str, str, str]]:
    return [(r.product_score.product_id, r.recommended.offer_id, r.recommended.mode_id)
            for r in recommend(candidates, intent, selection_policy="lever_score", **levers)]


def _mixed_fixture(commission_a=None, commission_b=None, uniform=None):
    """PA 两条报价（佣金/服务/价格各异）+ PB 一条报价，用于零实际处理对照。"""
    pa_a = _override_offer("Oa", "PA", price=30000, merchant_id="M001",
                           commission=uniform if uniform is not None else commission_a)
    pa_b = _override_offer("Ob", "PA", price=50000, merchant_id="M002",
                           commission=uniform if uniform is not None else commission_b)
    pb = _override_offer("Oc", "PB", price=20000, merchant_id="M003",
                         commission=uniform)
    merchants = {"M001": _merchant_full("M001", service=95),
                 "M002": _merchant_full("M002", service=60),
                 "M003": _merchant_full("M003", service=80)}
    pa = _cand("PA", metrics=_metrics("PA", comfort_score=80), offers=[pa_a, pa_b], merchants=merchants)
    pb_cand = _cand("PB", metrics=_metrics("PB", comfort_score=40), offers=[pb], merchants=merchants)
    return [pa, pb_cand]


def test_zero_treatments_match_the_lever_score_00_cell():
    """阴性对照（裁定1）：三种零实际处理——μ>0 且 tilt 等于原商品权重、
    统一佣金（λ·c<1）、λ=1e-12 且佣金全未知——所选报价与商品顺序都须与
    lever_score 的 00 格一致（选择规则不因这些处理切换）。"""
    intent = _intent(preferences={"price": .6, "comfort": .4}, budget=100000)
    baseline = _selection(_mixed_fixture(), intent)
    assert baseline == [("PA", "Oa", "bt"), ("PB", "Oc", "bt")]  # 杠杆00格：按分最高方案

    # μ>0 但 tilt 归一化后等于原商品权重：applied == weights，分数与选择不变
    identity_tilt = _selection(
        _mixed_fixture(), intent, intent_distortion=IntentDistortion(.8, {"price": .6, "comfort": .4}))
    assert identity_tilt == baseline

    # 统一佣金：headroom/additive/proportional 在 λc<1 时对 B 单调，选择不变
    for form in ("headroom", "additive", "proportional"):
        uniform = _selection(_mixed_fixture(uniform=.2), intent, platform_bias=.6, bias_form=form)
        assert uniform == baseline

    # λ=1e-12 且佣金全未知：实际零处理，不得切换选择（审验反例 λ=1e-12 场景）
    unknown_commission = _selection(_mixed_fixture(), intent, platform_bias=1e-12)
    assert unknown_commission == baseline


def test_lever_score_00_cell_is_marked_steered_and_explains_by_leverage():
    """lever_score 的 00 基线格也标记 platform_steered（选择按杠杆分=用户分、
    并列按价格决胜，可能不是服务分最高），供解释层按实验格式如实陈述。"""
    candidates = _mixed_fixture()
    intent = _intent(preferences={"price": .6, "comfort": .4}, budget=100000)
    results = recommend(candidates, intent, selection_policy="lever_score")
    assert all(r.platform_steered is True for r in results)


def test_score_product_recomputation_is_consistent_with_recommend():
    cheap = _override_offer("O010", price=30000, merchant_id="M003", min_days=8)
    premium = _override_offer("O011", price=80000, merchant_id="M001", min_days=1)
    merchants = {"M003": _merchant_full("M003", service=50, return_days=7, opened=False, warranty=3),
                 "M001": _merchant_full("M001", service=95, return_days=30, opened=True, warranty=24)}
    candidate = _cand(metrics=_metrics("P001", comfort_score=90), offers=[cheap, premium], merchants=merchants)
    intent = _intent(preferences={"price": .6, "comfort": .4, "merchant_service": 1}, budget=100000)
    result = recommend([candidate], intent)[0]
    direct = score_product(candidate, intent, result.recommended.plan)
    assert direct.score == pytest.approx(result.product_score.score)
    assert direct.selected_plan is result.recommended.plan


def test_recommend_requires_eligible_plans():
    empty = Candidate(product=_product("P009"), metrics=_metrics("P009"), eligible_plans=[])
    with pytest.raises(ValueError, match="缺少合格报价方案"):
        recommend([empty], _intent(budget=100000))


# ---------------------------------------------------------------------------
# S23-R1（ACCEPT-S2.3 整改）：开封退货政策未知不能按最佳售后计分

# simulated 来源；任一未知/无来源 → 整维缺失中性（0.5+MISSING_SOFT_METRIC）。
# ---------------------------------------------------------------------------

def test_unknown_opened_return_is_missing_via_merchant_default():
    # 合法批次形态：days=30 已知、opened=null —— 不得与"确认可开封"同分
    merchant = _merchant_full(return_days=30, opened=None)
    result = rank_offers([_plan(_product(), _override_offer(), merchant)],
                         _intent(preferences={"returns": 1}, budget=100000))[0]
    returns = _component(result, "returns")
    assert returns.missing and returns.raw_value is None
    assert returns.normalized_score == .5
    assert result.score == pytest.approx(50)
    assert "MISSING_SOFT_METRIC" in _codes(result)


def test_unknown_opened_return_is_missing_via_offer_override():
    # 覆盖对象整体优先路径：override days=30、opened=null
    offer = _override_offer(return_override={"days": 30, "opened_return_allowed": None,
                                             "return_shipping_fee_cents": 0, "conditions": "仅未开封",
                                             "exceptions": []})
    result = rank_offers([_plan(_product(), offer, _merchant_full())],
                         _intent(preferences={"returns": 1}, budget=100000))[0]
    returns = _component(result, "returns")
    assert returns.missing and returns.raw_value is None
    assert returns.normalized_score == .5
    assert result.score == pytest.approx(50)
    assert "MISSING_SOFT_METRIC" in _codes(result)


def test_not_collected_opened_source_is_missing():
    # 合法批次来源形态：opened 叶来源存在但 missing_reason=not_collected
    merchant = _merchant_full(return_days=30, opened=True)
    merchant["field_sources"]["default_return_policy.opened_return_allowed"] = {
        **_source("M001.default_return_policy.opened_return_allowed"), "missing_reason": "not_collected"}
    result = rank_offers([_plan(_product(), _override_offer(), merchant)],
                         _intent(preferences={"returns": 1}, budget=100000))[0]
    assert _component(result, "returns").missing
    assert result.score == pytest.approx(50)


def test_unsourced_opened_flag_is_missing():
    # opened 有值但叶来源缺失：与"days 无来源"同等处理，不按已知计分
    merchant = _merchant_full(return_days=30, opened=True)
    del merchant["field_sources"]["default_return_policy.opened_return_allowed"]
    result = rank_offers([_plan(_product(), _override_offer(), merchant)],
                         _intent(preferences={"returns": 1}, budget=100000))[0]
    assert _component(result, "returns").missing
    assert result.score == pytest.approx(50)


def test_cheaper_unknown_policy_offer_must_not_beat_confirmed_openable():
    # 验收场景回归：O004 更便宜（70900<71900）但开封政策未知；O003 确认 30 天可开封
    confirmed = _override_offer("O003", price=71900, merchant_id="M002", min_days=1)
    unknown = _override_offer("O004", price=70900, merchant_id="M003", min_days=1)
    merchants = {"M002": _merchant_full("M002", service=82, return_days=30, opened=True, warranty=12),
                 "M003": _merchant_full("M003", service=71, return_days=30, opened=None, warranty=12)}
    candidate = _cand(offers=[confirmed, unknown], merchants=merchants)
    intent = _intent(preferences={"returns": 1}, budget=80000)
    ranked = rank_offers(candidate.eligible_plans, intent)
    assert _component(ranked[0], "returns").normalized_score == pytest.approx(1.0)
    assert _component(ranked[1], "returns").missing and _component(ranked[1], "returns").normalized_score == .5
    result = recommend([candidate], intent)[0]
    assert result.recommended.offer_id == "O003"  # 未知开封不得胜过确认可开封


def test_known_opened_flags_still_score_full_and_half():
    # 对照：确认可开封=满分、确认不可开封=折半（原行为保持）
    opened_ok = _plan(_product(), _override_offer(), _merchant_full(opened=True))
    no_opened = _plan(_product(), _override_offer("O002", merchant_id="M002"), _merchant_full("M002", opened=False))
    ok, bad = rank_offers([opened_ok, no_opened], _intent(preferences={"returns": 1}, budget=100000))
    assert _component(ok, "returns").normalized_score == pytest.approx(1.0) and not _component(ok, "returns").missing
    assert _component(bad, "returns").normalized_score == pytest.approx(.5) and not _component(bad, "returns").missing
    assert ok.score == pytest.approx(100) and bad.score == pytest.approx(50)
