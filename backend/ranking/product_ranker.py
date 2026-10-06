"""Product ranking.v1; rules and limits are frozen in docs/ranking_baseline.md.

Only S2.1 eligible plans enter this module. All price/spec/commission features
belong to the selected plan. S2.3 can call score_product after choosing an Offer.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from backend.schemas.agent import PREFERENCE_DIMENSIONS, ShoppingIntent, WIRELESS_TRANSPORTS, WIRED_TRANSPORTS
from backend.services.candidate_service import Candidate
from backend.services.derive import OfferPlan

ALGORITHM_VERSION = "product-ranking.v1"
LEVER_VERSION = "platform-levers.v1"
METRICS = {key: f"{key}_score" for key in (
    "comfort", "anc", "sound_quality", "call", "gaming", "portability", "commute", "fit_stability",
)}
SUPPORTED = frozenset((*METRICS, "price", "battery", "weight", "wireless", "foldable"))
BALANCED = {"price": .20, "comfort": .25, "sound_quality": .25, "anc": .10, "battery": .10, "weight": .10}
PRICE_DENOMINATOR_DEFAULT = 200000  # cents; price scale B when the intent has no budget
WEIGHT_LIMITS = {"headset": 450.0, "single_earbud": 12.0, "pair": 24.0, "with_case": 150.0}

# pre-bias ranking score B. All three leave rankings unchanged under uniform c.
BIAS_FORMS = ("headroom", "additive", "proportional")


@dataclass(frozen=True)
class IntentDistortion:
    """Public Demo implementation."""

    strength: float
    tilt: dict

    def __post_init__(self):
        _number(self.strength, "intent_distortion.strength", high=1)
        if not isinstance(self.tilt, dict) or not self.tilt:
            raise ValueError("intent_distortion.tilt: 需要非空维度映射")
        for key, value in self.tilt.items():
            if key not in SUPPORTED:
                raise ValueError(f"intent_distortion.tilt: {key}不是评分维度")
            _number(value, f"intent_distortion.tilt.{key}")
        total = math.fsum(self.tilt.values())
        if total <= 0:
            raise ValueError("intent_distortion.tilt: 权重合计须大于0")
        object.__setattr__(self, "tilt", {key: self.tilt[key] / total for key in sorted(self.tilt) if self.tilt[key] > 0})


@dataclass(frozen=True)
class ScoreComponent:
    dimension: str
    raw_value: object
    unit: str | None
    basis: str | None
    normalized_score: float
    weight: float
    contribution: float
    missing: bool
    source_refs: list[str]
    # Weight actually used for ranking; equals weight unless intent distortion is on.
    applied_weight: float
    applied_contribution: float
    # Comparability metadata for battery/weight specs (S2A-05): two components
    # may only be compared when basis AND these leaves match exactly. Other
    # dimensions leave them None.
    anc_state: str | None = None
    mode_id: str | None = None
    condition_text: str | None = None


@dataclass
class ProductScore:
    product_id: str
    candidate: Candidate
    selected_plan: OfferPlan
    score: float
    base_score: float
    components: list[ScoreComponent]
    weights: dict[str, float]
    selected_specs: dict[str, str | None]
    bias_contribution: float
    commission_rate: float | None
    commission_missing: bool
    platform_bias: float
    algorithm_version: str
    warnings: list[dict]
    # score = base_score + distortion_contribution + bias_contribution
    applied_weights: dict[str, float]
    applied_base_score: float
    distortion_contribution: float
    intent_distortion: float
    bias_form: str
    lever_version: str


def _number(value, label: str, *, low: float = 0, high: float | None = None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}: 必须为有限数字")
    try:
        valid = math.isfinite(value) and value >= low and (high is None or value <= high)
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"{label}: 数值超出有效范围")
    return value


def _known(entity: dict, paths: list[str], *, simulated: bool = False) -> bool:
    sources = entity.get("field_sources") or {}
    for path in paths:
        source = sources.get(path)
        if not isinstance(source, dict) or source.get("missing_reason") is not None:
            return False
        if not all(source.get(key) for key in ("source_type", "source_ref", "observed_at", "method")):
            return False
        if simulated and source.get("source_type") != "simulated":
            return False
    return True


def _weights(intent: ShoppingIntent) -> tuple[dict[str, float], list[dict]]:
    prefs = intent.preferences.model_dump()
    warnings = [
        {"code": "UNSUPPORTED_PREFERENCE", "field": f"preferences.{key}",
         "message": "此维度未进入product-ranking.v1商品评分。"}
        for key in PREFERENCE_DIMENSIONS if key not in SUPPORTED and prefs[key] > 0
    ]
    requested = {key: prefs[key] for key in PREFERENCE_DIMENSIONS if key in SUPPORTED and prefs[key] > 0}
    if not requested:
        requested = BALANCED
        if warnings:
            warnings.append({"code": "DEFAULT_WEIGHTS", "field": "preferences",
                             "message": "未表达支持维度，使用balanced预设。"})
    total = math.fsum(requested.values())
    return {key: requested.get(key, 0.0) / total for key in PREFERENCE_DIMENSIONS}, warnings


def _applied_weights(weights: dict[str, float], distortion: IntentDistortion | None) -> dict[str, float]:
    if distortion is None or distortion.strength == 0:
        return dict(weights)
    mu = distortion.strength
    return {key: (1 - mu) * weights[key] + mu * distortion.tilt.get(key, 0.0) for key in PREFERENCE_DIMENSIONS}


def _check_levers(platform_bias, intent_distortion, bias_form) -> None:
    _number(platform_bias, "platform_bias", high=1)
    if intent_distortion is not None and not isinstance(intent_distortion, IntentDistortion):
        raise ValueError("intent_distortion: 需要IntentDistortion或None")
    if bias_form not in BIAS_FORMS:
        raise ValueError(f"bias_form: 须为{BIAS_FORMS}之一")


def _bias(bias_form: str, platform_bias: float, commission: float, score: float) -> float:
    strength = platform_bias * commission
    if bias_form == "headroom":
        return strength * (100.0 - score)
    if bias_form == "additive":
        return strength * 100.0
    return strength * score


def _validate_plan(candidate: Candidate, plan: OfferPlan) -> tuple[dict, int]:
    if not plan.eligible or plan.reasons or plan.landed is None:
        raise ValueError(f"{candidate.product_id}: 需要S2.1合格且可定价的方案")
    _number(plan.landed, "landed_price_cents")
    if (plan.offer.get("product_id") != candidate.product_id
            or plan.offer.get("merchant_id") != plan.merchant.get("merchant_id")):
        raise ValueError("评分方案商品/商家引用不一致")
    for index, mode in enumerate(candidate.product.get("connection_modes") or []):
        if mode.get("mode_id") == plan.mode_id and mode.get("playback_supported") is True:
            return mode, index
    raise ValueError("评分方案没有对应的可播放连接模式")


def _select_spec(product: dict, intent: ShoppingIntent, mode: dict, kind: str):
    """Return (spec, index, basis); retain hard-filter specs even at zero weight."""
    hc = intent.hard_constraints
    if kind == "battery":
        requirement = hc.battery_requirement
        basis = requirement.basis if requirement else "single_charge"
        if requirement is None:
            mode_index = (product.get("connection_modes") or []).index(mode)
            if (not isinstance(mode.get("active_noise_cancelling"), bool)
                    or not _known(product, [f"connection_modes[{mode_index}].active_noise_cancelling", "form_factor"])
                    or product.get("form_factor") not in ("over_ear", "on_ear", "in_ear", "open_ear")):
                return None, None, basis
        anc = requirement.anc_state if requirement else (
            "on" if mode.get("active_noise_cancelling") is True else "not_applicable"
        )
        collection, id_key = "battery_specs", "battery_spec_id"
    else:
        requirement = hc.weight_requirement
        basis = requirement.basis if requirement else {
            "over_ear": "headset", "on_ear": "headset", "in_ear": "single_earbud", "open_ear": "single_earbud",
        }.get(product.get("form_factor"))
        collection, id_key = "weight_specs", "weight_spec_id"
    specs = sorted(enumerate(product.get(collection) or []), key=lambda pair: str(pair[1].get(id_key, "")))
    for index, spec in specs:
        if not spec.get(id_key) or spec.get("basis") != basis:
            continue
        if kind == "battery":
            if spec.get("mode_id") != mode.get("mode_id") or spec.get("anc_state") != anc:
                continue
            if spec.get("hours") is None:
                continue
            if requirement is None and not (spec.get("test_conditions") or "").strip():
                continue
            if requirement and (
                (requirement.mode_id is not None and spec.get("mode_id") != requirement.mode_id)
                or spec["hours"] < requirement.min_hours
                or (requirement.conditions is not None and spec.get("test_conditions") != requirement.conditions)
            ):
                continue
        else:
            if not (spec.get("conditions") or "").strip() or spec.get("grams") is None:
                continue
            if requirement and spec["grams"] > requirement.max_grams:
                continue
        leaves = ("hours", "basis", "anc_state", "test_conditions") if kind == "battery" else ("grams", "basis", "conditions")
        if kind == "battery" and requirement and requirement.conditions is None:
            # S2.1's numeric threshold did not require a particular test condition.
            # Retain its record ID; soft scoring below still needs comparable conditions.
            leaves = ("hours", "basis", "anc_state")
        paths = [f"{collection}[{index}].{leaf}" for leaf in leaves]
        # A hard-filter record can remain selected with real evidence, while its
        # soft value has no comparable demo protocol. Soft-only unknown records
        # were not used in scoring and must not be advertised as selected specs.
        if not _known(product, paths, simulated=requirement is None):
            continue
        return spec, index, basis
    return None, None, basis


def _feature(candidate, intent, plan, mode, mode_index, dimension, specs):
    """Return raw, normalized, unit, basis, refs; raw=None means unknown."""
    product, metrics, pid = candidate.product, candidate.metrics or {}, candidate.product_id
    mode_prefix = f"connection_modes[{mode_index}]"
    if dimension == "price":
        denominator = intent.budget.max_cents or PRICE_DENOMINATOR_DEFAULT
        return plan.landed, max(0.0, min(1.0, 1 - plan.landed / denominator)), "CNY_cent", "landed_price", [f"{plan.offer['offer_id']}.landed_price_cents"]
    if dimension in ("battery", "weight"):
        spec, index, basis = specs[dimension]
        unit = "h" if dimension == "battery" else "g"
        if spec is None:
            return None, .5, unit, basis, []
        collection = f"{dimension}_specs"
        leaves = ("hours", "basis", "anc_state", "test_conditions") if dimension == "battery" else ("grams", "basis", "conditions")
        paths = [f"{collection}[{index}].{leaf}" for leaf in leaves]
        refs = [f"{pid}.{path}" for path in paths]
        conditions_key = "test_conditions" if dimension == "battery" else "conditions"
        if not (spec.get(conditions_key) or "").strip() or not _known(product, paths, simulated=True):
            return None, .5, unit, basis, refs
        if dimension == "battery":
            if basis != "with_case" and (
                product.get("form_factor") not in ("over_ear", "on_ear", "in_ear", "open_ear")
                or not _known(product, ["form_factor"])
            ):
                return None, .5, unit, basis, refs
            raw = _number(spec["hours"], "battery.hours")
            limit = 48 if basis == "with_case" else (12 if product.get("form_factor") in ("in_ear", "open_ear") else 48)
            normalized = raw / limit
        else:
            raw = _number(spec["grams"], "weight.grams")
            normalized = 1 - raw / WEIGHT_LIMITS[basis]
        return raw, max(0.0, min(1.0, normalized)), unit, basis, refs
    if dimension in ("anc", "call", "wireless"):
        key = {"anc": "active_noise_cancelling", "call": "microphone_supported", "wireless": "transport"}[dimension]
        path = f"{mode_prefix}.{key}"
        refs = [f"{pid}.{path}"]
        value = mode.get(key)
        if value is None or not _known(product, [path]):
            return None, .5, "boolean", plan.mode_id, refs
        if dimension == "wireless":
            if value in WIRELESS_TRANSPORTS:
                return True, 1.0, "boolean", plan.mode_id, refs
            if value in WIRED_TRANSPORTS:
                return False, 0.0, "boolean", plan.mode_id, refs
            return None, .5, "boolean", plan.mode_id, refs
        if value is False:
            return False, 0.0, "boolean", plan.mode_id, refs
        if value is not True:
            return None, .5, "boolean", plan.mode_id, refs
    if dimension == "foldable":
        refs = [f"{pid}.foldable"]
        value = product.get("foldable")
        if isinstance(value, bool) and _known(product, ["foldable"]):
            return value, float(value), "boolean", None, refs
        return None, .5, "boolean", None, refs
    field = METRICS[dimension]
    refs = [f"{pid}.metrics.{field}"]
    if dimension in ("anc", "call"):
        key = "active_noise_cancelling" if dimension == "anc" else "microphone_supported"
        refs.insert(0, f"{pid}.{mode_prefix}.{key}")
    raw = metrics.get(field)
    if raw is None or not _known(metrics, [field], simulated=True):
        return None, .5, "score_100", "simulated", refs
    return raw, _number(raw, field, high=100) / 100, "score_100", "simulated", refs


def _specs_for(candidate: Candidate, intent: ShoppingIntent, mode: dict, needed) -> tuple[dict, dict]:
    specs = {}
    selected_specs = {"battery_spec_id": None, "weight_spec_id": None}
    for kind in ("battery", "weight"):
        requirement = getattr(intent.hard_constraints, f"{kind}_requirement")
        specs[kind] = _select_spec(candidate.product, intent, mode, kind) if needed(kind) or requirement else (None, None, None)
        spec = specs[kind][0]
        if spec is not None:
            selected_specs[f"{kind}_spec_id"] = spec[f"{kind}_spec_id"]
    return specs, selected_specs


def plan_features(candidate: Candidate, intent: ShoppingIntent, plan: OfferPlan) -> dict[str, float | None]:
    """Normalized 0-1 value of every supported dimension for one plan; None = unknown."""
    mode, mode_index = _validate_plan(candidate, plan)
    specs, _ = _specs_for(candidate, intent, mode, lambda kind: True)
    result = {}
    for dimension in sorted(SUPPORTED):
        raw, normalized, *_ = _feature(candidate, intent, plan, mode, mode_index, dimension, specs)
        result[dimension] = None if raw is None else normalized
    return result


def score_product(candidate: Candidate, intent: ShoppingIntent, plan: OfferPlan, platform_bias: float = 0.0, *,
                  intent_distortion: IntentDistortion | None = None, bias_form: str = "headroom") -> ProductScore:
    """Public Demo implementation."""
    _check_levers(platform_bias, intent_distortion, bias_form)
    mode, mode_index = _validate_plan(candidate, plan)
    if candidate.metrics and candidate.metrics.get("product_id") != candidate.product_id:
        raise ValueError("指标与商品引用不一致")
    weights, warnings = _weights(intent)
    applied = _applied_weights(weights, intent_distortion)
    specs, selected_specs = _specs_for(candidate, intent, mode, lambda kind: weights[kind] > 0 or applied[kind] > 0)
    components = []
    for dimension in PREFERENCE_DIMENSIONS:
        weight, applied_weight = weights[dimension], applied[dimension]
        if weight == 0 and applied_weight == 0:
            continue
        raw, normalized, unit, basis, refs = _feature(candidate, intent, plan, mode, mode_index, dimension, specs)
        missing = raw is None
        comparability: dict[str, str | None] = {}
        if dimension in ("battery", "weight"):
            spec = specs[dimension][0]
            if spec is not None:
                if dimension == "battery":
                    comparability = {
                        "anc_state": spec.get("anc_state"),
                        "mode_id": spec.get("mode_id"),
                        "condition_text": spec.get("test_conditions") or None,
                    }
                else:
                    comparability = {"condition_text": spec.get("conditions") or None}
        components.append(ScoreComponent(dimension, raw, unit, basis, normalized, weight, normalized * weight * 100,
                                          missing, refs, applied_weight, normalized * applied_weight * 100,
                                          **comparability))
        if missing:
            warnings.append({"code": "MISSING_SOFT_METRIC", "product_id": candidate.product_id,
                             "field": dimension, "message": "软指标未知，使用固定中性0.5；未更改原始事实。"})
    base_score = min(100.0, math.fsum(c.contribution for c in components))
    applied_base = min(100.0, math.fsum(c.applied_contribution for c in components))
    commission = plan.offer.get("commission_rate")
    if commission is not None:
        _number(commission, "commission_rate", high=1)
    if commission is None or not _known(plan.offer, ["commission_rate"]):
        commission = None
        if platform_bias > 0:
            warnings.append({"code": "MISSING_COMMISSION", "product_id": candidate.product_id,
                             "field": f"{plan.offer['offer_id']}.commission_rate", "message": "佣金未知，不提升曝光分。"})
    bias = 0.0 if commission is None else _bias(bias_form, platform_bias, commission, applied_base)
    return ProductScore(candidate.product_id, candidate, plan, applied_base + bias, base_score,
                        components, weights, selected_specs, bias, commission, commission is None,
                        platform_bias, ALGORITHM_VERSION, warnings, applied, applied_base,
                        applied_base - base_score, 0.0 if intent_distortion is None else intent_distortion.strength,
                        bias_form, LEVER_VERSION)


def rank_products(candidates: list[Candidate], intent: ShoppingIntent, platform_bias: float = 0.0, *,
                  intent_distortion: IntentDistortion | None = None, bias_form: str = "headroom") -> list[ProductScore]:
    """Score eligible plans, choose one per product, then stably order products.

    The per-product plan is chosen by the same ranking score, so under active
    levers the platform also steers which merchant's Offer gets the exposure.
    """
    _check_levers(platform_bias, intent_distortion, bias_form)
    result, seen = [], set()
    for candidate in candidates:
        if candidate.product_id in seen:
            raise ValueError(f"重复商品: {candidate.product_id}")
        seen.add(candidate.product_id)
        if not candidate.eligible_plans:
            raise ValueError(f"{candidate.product_id}: 缺少合格报价方案")
        scores = [score_product(candidate, intent, plan, platform_bias,
                                intent_distortion=intent_distortion, bias_form=bias_form)
                  for plan in candidate.eligible_plans]
        result.append(min(scores, key=lambda s: (-s.score, s.selected_plan.landed,
                                                s.selected_plan.offer["offer_id"], s.selected_plan.mode_id)))
    return sorted(result, key=lambda s: (-s.score, s.product_id))
