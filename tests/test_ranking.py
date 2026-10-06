"""S2.2 acceptance tests from the documented product-ranking.v1 baseline.

Fixtures use real Candidate and derive.build_plan objects, without a database or
the demo snapshot. Expected scores are small, independently calculable examples.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from itertools import permutations
import math

import pytest

from backend.ranking.product_ranker import (
    ALGORITHM_VERSION,
    rank_products,
    score_product,
)
from backend.schemas.agent import (
    PREFERENCE_DIMENSIONS,
    BatteryRequirement,
    Budget,
    HardConstraints,
    Preferences,
    ShoppingIntent,
    WeightRequirement,
)
from backend.services.candidate_service import Candidate
from backend.services.derive import CN_TZ, build_plan


OBSERVED = "2026-09-30T10:00:00+08:00"
NOW = datetime(2026, 9, 30, 12, tzinfo=CN_TZ)
METRIC_FIELDS = (
    "comfort_score", "sound_quality_score", "anc_score", "call_score",
    "gaming_score", "portability_score", "commute_score", "fit_stability_score",
)


def _source(ref: str, *, source_type: str = "simulated") -> dict:
    return {
        "source_type": source_type,
        "source_ref": ref,
        "observed_at": OBSERVED,
        "method": "fixed hand-calculable fixture",
        "missing_reason": None,
    }


def _mode(mode_id="bt", transport="bluetooth", *, anc=True, microphone=True):
    return {
        "mode_id": mode_id, "transport": transport,
        "playback_supported": True, "active_noise_cancelling": anc,
        "microphone_supported": microphone, "requires_power": True,
        "required_accessory_codes": [],
    }


def _battery(spec_id="B1", hours=24, *, basis="single_charge", anc="on",
             mode="bt", conditions="simulated reference listening"):
    return {
        "battery_spec_id": spec_id, "hours": hours, "basis": basis,
        "anc_state": anc, "mode_id": mode, "test_conditions": conditions,
    }


def _weight(spec_id="W1", grams=225, *, basis="headset",
            conditions="complete headset without accessories"):
    return {
        "weight_spec_id": spec_id, "grams": grams, "basis": basis,
        "conditions": conditions,
    }


def _product_sources(product: dict) -> dict:
    pid = product["product_id"]
    paths = ["form_factor", "foldable", "active_noise_cancelling", "microphone", "wireless"]
    for index, mode in enumerate(product["connection_modes"]):
        paths.extend(f"connection_modes[{index}].{key}" for key in mode if key != "mode_id")
    for collection, leaves in (
        ("battery_specs", ("hours", "basis", "anc_state", "test_conditions")),
        ("weight_specs", ("grams", "basis", "conditions")),
    ):
        for index, _ in enumerate(product.get(collection) or []):
            paths.extend(f"{collection}[{index}].{leaf}" for leaf in leaves)
    return {path: _source(f"{pid}.{path}") for path in paths}


def _product(product_id="P001", *, form="over_ear", modes=None,
             batteries=None, weights=None) -> dict:
    product = {
        "product_id": product_id, "form_factor": form,
        "active_noise_cancelling": True, "microphone": True, "wireless": True,
        "foldable": True, "connection_modes": modes if modes is not None else [_mode()],
        "battery_specs": batteries if batteries is not None else [_battery()],
        "weight_specs": weights if weights is not None else [_weight()],
    }
    product["field_sources"] = _product_sources(product)
    return product


def _metrics(product_id="P001", **values) -> dict:
    metrics = {
        "product_id": product_id, "comfort_score": 80,
        "sound_quality_score": 60, "anc_score": 40, "call_score": 70,
        "gaming_score": 30, "portability_score": 55, "commute_score": 85,
        "fit_stability_score": 65,
    }
    metrics.update(values)
    metrics["field_sources"] = {
        key: _source(f"{product_id}.metrics.{key}") for key in METRIC_FIELDS
    }
    return metrics


def _offer(offer_id="O001", product_id="P001", *, price=50000, shipping=0,
           discount=0, commission=None, merchant_id="M001") -> dict:
    offer = {
        "offer_id": offer_id, "product_id": product_id, "merchant_id": merchant_id,
        "price_cents": price, "shipping_fee_cents": shipping,
        "instant_discount_cents": discount, "stock": 10, "valid_until": None,
        "accessories": [], "return_policy_override": None, "warranty_override": None,
        "commission_rate": commission,
        "delivery": {
            "min_days": 1, "max_days": 2, "commitment_type": "guaranteed",
            "destination_region": "CN-SH", "conditions": "reference shipment",
        },
    }
    paths = ["price_cents", "shipping_fee_cents", "instant_discount_cents", "commission_rate"]
    paths.extend(f"delivery.{key}" for key in offer["delivery"])
    offer["field_sources"] = {path: _source(f"{offer_id}.{path}") for path in paths}
    return offer


def _merchant(merchant_id="M001") -> dict:
    return {
        "merchant_id": merchant_id,
        "default_return_policy": {
            "days": 30, "opened_return_allowed": True,
            "return_shipping_fee_cents": 0, "conditions": "reference policy", "exceptions": [],
        },
        "default_warranty": {"months": 12, "provider": "merchant", "conditions": "reference warranty"},
        "field_sources": {},
    }


def _candidate(product_id="P001", *, product=None, metrics=None, offers=None,
               mode_ids=None) -> Candidate:
    product = product if product is not None else _product(product_id)
    metrics = metrics if metrics is not None else _metrics(product["product_id"])
    offers = offers if offers is not None else [_offer(product_id=product["product_id"])]
    mode_ids = mode_ids if mode_ids is not None else [m["mode_id"] for m in product["connection_modes"]]
    plans = [
        build_plan(product, offer, _merchant(offer["merchant_id"]), mode_id=mode_id,
                   region="CN-SH", now=NOW)
        for offer in offers for mode_id in mode_ids
    ]
    assert all(plan.eligible for plan in plans), "fixture must represent an S2.1 eligible candidate"
    return Candidate(product=product, metrics=metrics, eligible_plans=plans)


def _intent(*, preferences=None, budget=None, hard=None) -> ShoppingIntent:
    return ShoppingIntent(
        preferences=Preferences(**(preferences or {})),
        budget=Budget(max_cents=budget), hard_constraints=hard or HardConstraints(),
    )


def _component(result, dimension):
    return next(component for component in result.components if component.dimension == dimension)


def _warning_codes(result):
    return {warning["code"] for warning in result.warnings}


def test_balanced_baseline_is_hand_calculable_and_reconstructable():
    candidate = _candidate()
    result = rank_products([candidate], _intent())[0]
    # .75*.20 + .80*.25 + .60*.25 + .40*.10 + .50*.10 + .50*.10 = .64
    assert result.base_score == pytest.approx(64)
    assert result.score == pytest.approx(64)
    assert math.fsum(c.contribution for c in result.components) == pytest.approx(64)
    assert result.bias_contribution == 0
    assert set(result.weights) == set(PREFERENCE_DIMENSIONS)
    assert sum(result.weights.values()) == pytest.approx(1)
    assert result.weights["shipping"] == 0
    assert result.selected_plan is candidate.eligible_plans[0]
    assert result.candidate is candidate
    assert result.product_id == candidate.product_id
    assert result.selected_specs == {"battery_spec_id": "B1", "weight_spec_id": "W1"}
    assert result.algorithm_version == ALGORITHM_VERSION == "product-ranking.v1"
    assert all(c.source_refs for c in result.components if c.weight > 0 and not c.missing)
    with pytest.raises(FrozenInstanceError):
        result.components[0].weight = 0


def test_explicit_preferences_replace_preset_and_sum_to_one():
    result = rank_products([_candidate()], _intent(preferences={"price": .25, "comfort": .75}, budget=100000))[0]
    assert result.base_score == pytest.approx(72.5)  # .5*.25*100 + .8*.75*100
    assert result.weights["price"] == pytest.approx(.25)
    assert result.weights["comfort"] == pytest.approx(.75)
    assert all(weight == 0 for dimension, weight in result.weights.items() if dimension not in {"price", "comfort"})


def test_increasing_battery_preference_increases_its_contribution():
    candidate = _candidate()
    low = rank_products([candidate], _intent(preferences={"battery": .25, "comfort": .75}))[0]
    high = rank_products([candidate], _intent(preferences={"battery": .75, "comfort": .75}))[0]
    assert _component(low, "battery").contribution == pytest.approx(12.5)
    assert _component(high, "battery").contribution == pytest.approx(25)
    assert _component(high, "battery").normalized_score == _component(low, "battery").normalized_score


def test_missing_soft_metric_is_neutral_without_redistributing_weight():
    result = rank_products([_candidate(metrics=_metrics(comfort_score=None))], _intent(preferences={"comfort": .5, "price": .5}))[0]
    component = _component(result, "comfort")
    assert component.raw_value is None and component.missing is True
    assert component.normalized_score == .5
    assert component.weight == .5 and component.contribution == 25
    assert result.base_score == pytest.approx(62.5)
    assert "MISSING_SOFT_METRIC" in _warning_codes(result)


def test_mode_battery_binding_does_not_borrow_other_mode_or_anc_state():
    product = _product(modes=[_mode("bt"), _mode("usb", "usb_audio", anc=False)], batteries=[
        _battery("B1", 48, mode="bt"), _battery("B2", 12, mode="usb", anc="not_applicable"),
        _battery("B3", 48, mode="usb", anc="off"),
    ])
    candidate = _candidate(product=product, mode_ids=["usb"])
    result = rank_products([candidate], _intent(preferences={"battery": 1}))[0]
    assert result.selected_plan.mode_id == "usb"
    assert _component(result, "battery").raw_value == 12
    assert result.base_score == pytest.approx(25)
    assert result.selected_specs["battery_spec_id"] == "B2"


def test_default_battery_does_not_use_with_case_total():
    candidate = _candidate(product=_product(form="in_ear", batteries=[_battery(hours=48, basis="with_case")]))
    result = rank_products([candidate], _intent(preferences={"battery": 1}))[0]
    assert _component(result, "battery").missing is True
    assert result.base_score == 50
    assert result.selected_specs["battery_spec_id"] is None


def test_price_uses_landed_total_of_actual_selected_plan():
    offer = _offer(price=10000, shipping=1000, discount=500)
    result = rank_products([_candidate(offers=[offer])], _intent(preferences={"price": 1}, budget=20000))[0]
    assert result.selected_plan.landed == 10500
    assert _component(result, "price").raw_value == 10500
    assert result.base_score == pytest.approx(47.5)


def test_selected_plan_can_be_rescored_without_changing_the_result():
    candidate = _candidate(offers=[_offer("O001", commission=.2), _offer("O002", price=60000, commission=.8)])
    intent = _intent(preferences={"price": 1})
    ranked = rank_products([candidate], intent, platform_bias=.5)[0]
    rescored = score_product(candidate, intent, ranked.selected_plan, platform_bias=.5)
    assert rescored.score == ranked.score
    assert rescored.base_score == ranked.base_score
    assert rescored.bias_contribution == ranked.bias_contribution
    assert rescored.components == ranked.components
    assert rescored.weights == ranked.weights
    assert rescored.selected_specs == ranked.selected_specs


@pytest.mark.parametrize("dimension,field", [
    ("anc", "connection_modes[0].active_noise_cancelling"),
    ("call", "connection_modes[0].microphone_supported"),
    ("foldable", "foldable"),
])
def test_known_false_is_zero_but_source_missing_false_is_neutral(dimension, field):
    product = _product(modes=[_mode(anc=False, microphone=False)])
    product["foldable"] = False
    candidate = _candidate(product=product)
    intent = _intent(preferences={dimension: 1})
    known = rank_products([candidate], intent)[0]
    component = _component(known, dimension)
    assert component.raw_value is False
    assert component.normalized_score == 0 and component.missing is False
    assert known.base_score == 0
    assert component.source_refs
    del product["field_sources"][field]
    unknown = rank_products([candidate], intent)[0]
    component = _component(unknown, dimension)
    assert component.raw_value is None and component.missing is True
    assert component.normalized_score == .5 and unknown.base_score == 50
    assert "MISSING_SOFT_METRIC" in _warning_codes(unknown)


@pytest.mark.parametrize("dimension,mode", [
    ("anc", _mode(anc=None)), ("call", _mode(microphone=None)),
    ("wireless", _mode(transport="optical_transmitter")),
])
def test_unknown_mode_capability_does_not_inherit_product_true(dimension, mode):
    candidate = _candidate(product=_product(modes=[mode]))
    result = rank_products([candidate], _intent(preferences={dimension: 1}))[0]
    assert result.base_score == 50
    assert _component(result, dimension).missing is True
    assert _component(result, dimension).raw_value is None


@pytest.mark.parametrize("transport,expected", [
    ("bluetooth", 100), ("wireless_dongle", 100),
    ("wired_3_5mm", 0), ("usb_audio", 0),
])
def test_wireless_soft_score_uses_actual_mode_transport(transport, expected):
    result = rank_products([_candidate(product=_product(modes=[_mode(transport=transport)]))],
                           _intent(preferences={"wireless": 1}))[0]
    assert result.base_score == expected
    assert _component(result, "wireless").missing is False


@pytest.mark.parametrize("form,hours,grams,basis", [
    ("over_ear", 24, 225, "headset"), ("on_ear", 24, 225, "headset"),
    ("in_ear", 6, 6, "single_earbud"), ("open_ear", 6, 6, "single_earbud"),
])
def test_shape_specific_battery_and_weight_boundaries(form, hours, grams, basis):
    candidate = _candidate(product=_product(form=form, batteries=[_battery(hours=hours)],
                                           weights=[_weight(grams=grams, basis=basis)]))
    result = rank_products([candidate], _intent(preferences={"battery": 1, "weight": 1}))[0]
    assert _component(result, "battery").normalized_score == .5
    assert _component(result, "weight").normalized_score == .5
    assert _component(result, "battery").basis == "single_charge"
    assert _component(result, "weight").basis == basis
    assert result.base_score == 50


@pytest.mark.parametrize("basis,grams,max_grams", [("pair", 12, 20), ("with_case", 75, 100)])
def test_explicit_weight_basis_is_scored_without_cross_basis_conversion(basis, grams, max_grams):
    product = _product(form="in_ear", weights=[
        _weight("W0", 1, basis="single_earbud"), _weight("W1", grams, basis=basis),
    ])
    intent = _intent(preferences={"weight": 1}, hard=HardConstraints(
        weight_requirement=WeightRequirement(max_grams=max_grams, basis=basis)))
    result = rank_products([_candidate(product=product)], intent)[0]
    assert result.selected_specs["weight_spec_id"] == "W1"
    assert _component(result, "weight").basis == basis
    assert _component(result, "weight").raw_value == grams
    assert result.base_score == 50


def test_hard_specs_respect_threshold_conditions_and_lexicographic_selection():
    product = _product(batteries=[
        _battery("B0", 4, conditions="required condition"),
        _battery("B1", 48, conditions="different condition"),
        _battery("B9", 48, conditions="required condition"),
        _battery("B10", 12, conditions="required condition"),
    ], weights=[_weight("W0", 300), _weight("W9", 100), _weight("W10", 225)])
    hard = HardConstraints(
        battery_requirement=BatteryRequirement(min_hours=8, basis="single_charge", anc_state="on",
                                               mode_id="bt", conditions="required condition"),
        weight_requirement=WeightRequirement(max_grams=250, basis="headset"),
    )
    result = rank_products([_candidate(product=product)], _intent(preferences={"battery": 1, "weight": 1}, hard=hard))[0]
    assert result.selected_specs == {"battery_spec_id": "B10", "weight_spec_id": "W10"}
    assert _component(result, "battery").raw_value == 12
    assert _component(result, "weight").raw_value == 225
    assert result.base_score == pytest.approx(37.5)


def test_hard_specs_are_preserved_even_when_only_price_has_weight():
    product = _product(batteries=[_battery("B0", 4), _battery("B1", 12)],
                       weights=[_weight("W0", 300), _weight("W1", 225)])
    hard = HardConstraints(
        battery_requirement=BatteryRequirement(min_hours=8, basis="single_charge", anc_state="on"),
        weight_requirement=WeightRequirement(max_grams=250, basis="headset"),
    )
    result = rank_products([_candidate(product=product)], _intent(preferences={"price": 1}, hard=hard))[0]
    assert result.selected_specs == {"battery_spec_id": "B1", "weight_spec_id": "W1"}
    assert result.weights["battery"] == result.weights["weight"] == 0
    assert result.base_score == 75


def test_without_hard_requirement_zero_weight_specs_are_not_selected():
    result = rank_products([_candidate()], _intent(preferences={"price": 1}))[0]
    assert result.selected_specs == {"battery_spec_id": None, "weight_spec_id": None}


def test_with_case_is_used_only_when_explicitly_requested():
    product = _product(form="in_ear", batteries=[_battery("B0", 12), _battery("B1", 24, basis="with_case")])
    hard = HardConstraints(battery_requirement=BatteryRequirement(
        min_hours=20, basis="with_case", anc_state="on", mode_id="bt"))
    result = rank_products([_candidate(product=product)], _intent(preferences={"battery": 1}, hard=hard))[0]
    assert _component(result, "battery").basis == "with_case"
    assert _component(result, "battery").raw_value == 24
    assert result.selected_specs["battery_spec_id"] == "B1"
    assert result.base_score == 50


def test_default_spec_selection_is_lexicographic_not_best_test_condition():
    product = _product(batteries=[_battery("B9", 48), _battery("B10", 12)],
                       weights=[_weight("W9", 100), _weight("W10", 225)])
    result = rank_products([_candidate(product=product)], _intent(preferences={"battery": 1, "weight": 1}))[0]
    assert result.selected_specs == {"battery_spec_id": "B10", "weight_spec_id": "W10"}
    assert result.base_score == pytest.approx(37.5)


@pytest.mark.parametrize("dimension,field", [
    ("battery", "battery_specs[0].hours"),
    ("battery", "battery_specs[0].basis"),
    ("battery", "battery_specs[0].anc_state"),
    ("battery", "battery_specs[0].test_conditions"),
    ("weight", "weight_specs[0].grams"),
    ("weight", "weight_specs[0].basis"),
    ("weight", "weight_specs[0].conditions"),
])
def test_physical_specs_require_every_comparable_source_leaf(dimension, field):
    product = _product(batteries=[_battery(hours=48)], weights=[_weight(grams=1)])
    del product["field_sources"][field]
    result = rank_products([_candidate(product=product)], _intent(preferences={dimension: 1}))[0]
    assert _component(result, dimension).missing is True
    assert _component(result, dimension).raw_value is None
    assert result.selected_specs[f"{dimension}_spec_id"] is None
    assert result.base_score == 50


@pytest.mark.parametrize("dimension,field", [
    ("battery", "battery_specs[0].hours"), ("weight", "weight_specs[0].grams"),
])
def test_unregistered_real_measurement_protocol_is_neutral(dimension, field):
    product = _product(batteries=[_battery(hours=48)], weights=[_weight(grams=1)])
    product["field_sources"][field]["source_type"] = "measurement"
    result = rank_products([_candidate(product=product)], _intent(preferences={dimension: 1}))[0]
    assert _component(result, dimension).missing is True
    assert result.base_score == 50
    assert result.selected_specs[f"{dimension}_spec_id"] is None


@pytest.mark.parametrize("dimension,field", [
    ("battery", "battery_specs[0].hours"), ("weight", "weight_specs[0].grams"),
])
def test_hard_spec_id_remains_available_when_real_soft_protocol_is_not_comparable(dimension, field):
    product = _product()
    product["field_sources"][field]["source_type"] = "measurement"
    hard = HardConstraints(
        battery_requirement=BatteryRequirement(min_hours=20, basis="single_charge", anc_state="on"),
        weight_requirement=WeightRequirement(max_grams=250, basis="headset"),
    )
    result = rank_products([_candidate(product=product)], _intent(preferences={dimension: 1}, hard=hard))[0]
    assert _component(result, dimension).missing is True
    assert result.base_score == 50
    assert result.selected_specs == {"battery_spec_id": "B1", "weight_spec_id": "W1"}


def test_soft_spec_selection_skips_unusable_source_before_lexicographic_choice():
    product = _product(batteries=[_battery("B0", 48), _battery("B1", 24)],
                       weights=[_weight("W0", 1), _weight("W1", 225)])
    del product["field_sources"]["battery_specs[0].hours"]
    product["field_sources"]["weight_specs[0].grams"]["source_type"] = "measurement"
    result = rank_products([_candidate(product=product)], _intent(preferences={"battery": 1, "weight": 1}))[0]
    assert result.selected_specs == {"battery_spec_id": "B1", "weight_spec_id": "W1"}
    assert _component(result, "battery").raw_value == 24
    assert _component(result, "weight").raw_value == 225
    assert all(not component.missing for component in result.components)
    assert result.base_score == 50


@pytest.mark.parametrize("unknown_reason", ["unknown_value", "missing_source"])
def test_default_battery_requires_confirmed_mode_anc_state(unknown_reason):
    if unknown_reason == "unknown_value":
        product = _product(modes=[_mode(anc=None)], batteries=[_battery(hours=48, anc="not_applicable")])
    else:
        product = _product(batteries=[_battery(hours=48)])
        del product["field_sources"]["connection_modes[0].active_noise_cancelling"]
    result = rank_products([_candidate(product=product)], _intent(preferences={"battery": 1}))[0]
    assert _component(result, "battery").missing is True
    assert result.selected_specs["battery_spec_id"] is None
    assert result.base_score == 50


def test_rescoring_another_offer_changes_product_price_component():
    candidate = _candidate(offers=[_offer("O001", price=50000), _offer("O002", price=100000)])
    intent = _intent(preferences={"price": 1})
    selected = rank_products([candidate], intent)[0]
    other_plan = next(plan for plan in candidate.eligible_plans if plan.offer["offer_id"] == "O002")
    rescored = score_product(candidate, intent, other_plan)
    assert selected.base_score == 75
    assert rescored.selected_plan is other_plan
    assert _component(rescored, "price").raw_value == 100000
    assert rescored.base_score == 50
    assert rescored.weights == selected.weights


def test_necessary_accessory_cost_is_included_in_selected_plan_price():
    product = _product()
    product["connection_modes"][0]["required_accessory_codes"] = ["dongle"]
    offer = _offer(price=10000, shipping=1000, discount=500)
    offer["accessories"] = [{
        "accessory_code": "dongle", "included": False, "price_cents": 5000, "stock": 5,
    }]
    result = rank_products([_candidate(product=product, offers=[offer])],
                           _intent(preferences={"price": 1}, budget=20000))[0]
    assert result.selected_plan.accessory_total == 5000
    assert _component(result, "price").raw_value == 15500
    assert result.base_score == pytest.approx(22.5)


@pytest.mark.parametrize("dimension,collection,condition_key", [
    ("battery", "battery_specs", "test_conditions"), ("weight", "weight_specs", "conditions"),
])
def test_unknown_test_condition_or_weight_scope_is_neutral(dimension, collection, condition_key):
    product = _product()
    product[collection][0][condition_key] = None
    result = rank_products([_candidate(product=product)], _intent(preferences={dimension: 1}))[0]
    assert _component(result, dimension).missing is True
    assert result.base_score == 50


@pytest.mark.parametrize("source_type", [None, "measurement", "manufacturer"])
def test_product_metrics_need_simulated_field_evidence(source_type):
    metrics = _metrics(comfort_score=100)
    if source_type is None:
        del metrics["field_sources"]["comfort_score"]
    else:
        metrics["field_sources"]["comfort_score"]["source_type"] = source_type
    result = rank_products([_candidate(metrics=metrics)], _intent(preferences={"comfort": 1}))[0]
    assert _component(result, "comfort").raw_value is None
    assert _component(result, "comfort").missing is True
    assert result.base_score == 50


def test_unsupported_preferences_warn_and_do_not_change_supported_weights():
    candidate = _candidate()
    only_unsupported = rank_products([candidate], _intent(preferences={"shipping": 1, "low_latency": 1}))[0]
    assert only_unsupported.base_score == 64
    assert only_unsupported.weights["shipping"] == only_unsupported.weights["low_latency"] == 0
    assert "UNSUPPORTED_PREFERENCE" in _warning_codes(only_unsupported)
    mixed = rank_products([candidate], _intent(preferences={"price": .3, "shipping": 1}))[0]
    assert mixed.weights["price"] == 1 and mixed.base_score == 75
    assert "UNSUPPORTED_PREFERENCE" in _warning_codes(mixed)


def test_metrics_and_physical_boundaries_clamp_without_rounding():
    product = _product(batteries=[_battery(hours=60)], weights=[_weight(grams=600)])
    result = rank_products([_candidate(product=product)], _intent(preferences={"battery": 1, "weight": 1}))[0]
    assert _component(result, "battery").normalized_score == 1
    assert _component(result, "weight").normalized_score == 0
    precise = rank_products([_candidate(offers=[_offer(price=1)])], _intent(preferences={"price": 1}, budget=3))[0]
    assert precise.base_score == pytest.approx(100 * (1 - 1 / 3), abs=1e-12)
    assert precise.base_score != round(precise.base_score, 2)


def test_zero_budget_uses_finite_price_reference_for_zero_cost_eligible_plan():
    result = rank_products([_candidate(offers=[_offer(price=0)])], _intent(preferences={"price": 1}, budget=0))[0]
    assert result.base_score == 100
    assert math.isfinite(result.score)


def test_product_and_plan_ties_are_stable_across_input_permutations():
    product = _product(modes=[_mode("z"), _mode("a")])
    candidate = _candidate(product=product, offers=[
        _offer("O003", price=60000), _offer("O002", price=50000), _offer("O001", price=50000),
    ])
    intent = _intent(preferences={"comfort": 1})
    selected = rank_products([candidate], intent)[0]
    assert (selected.selected_plan.landed, selected.selected_plan.offer["offer_id"], selected.selected_plan.mode_id) == (50000, "O001", "a")
    reversed_plans = Candidate(product=candidate.product, metrics=candidate.metrics,
                               eligible_plans=list(reversed(candidate.eligible_plans)))
    assert rank_products([reversed_plans], intent)[0].selected_plan.mode_id == "a"
    assert rank_products([reversed_plans], intent)[0].selected_plan.offer["offer_id"] == "O001"
    candidates = [_candidate("P003"), _candidate("P001"), _candidate("P002")]
    for order in permutations(candidates):
        assert [result.product_id for result in rank_products(list(order), intent)] == ["P001", "P002", "P003"]


def test_irrelevant_candidate_does_not_change_existing_normalization_or_score():
    candidate = _candidate()
    intent = _intent(preferences={"price": .7, "battery": .3})
    original = rank_products([candidate], intent)[0]
    irrelevant = _candidate("P999", product=_product("P999", batteries=[_battery(hours=120)]),
                            offers=[_offer(product_id="P999", price=1)])
    changed = next(result for result in rank_products([irrelevant, candidate], intent) if result.product_id == "P001")
    assert changed.base_score == original.base_score
    assert changed.score == original.score
    assert changed.components == original.components
    assert changed.weights == original.weights


def test_each_offer_uses_its_own_price_and_commission_when_choosing_plan():
    candidate = _candidate(offers=[_offer("O001", price=50000, commission=0),
                                   _offer("O002", price=100000, commission=1)])
    intent = _intent(preferences={"price": 1})
    without_bias = rank_products([candidate], intent)[0]
    assert without_bias.selected_plan.offer["offer_id"] == "O001"
    assert without_bias.score == 75 and without_bias.bias_contribution == 0
    with_bias = rank_products([candidate], intent, platform_bias=.6)[0]
    assert with_bias.selected_plan.offer["offer_id"] == "O002"
    assert _component(with_bias, "price").raw_value == 100000
    assert with_bias.base_score == 50 and with_bias.commission_rate == 1
    assert with_bias.bias_contribution == 30 and with_bias.score == 80


def test_duplicate_connection_modes_do_not_multiply_offer_commission():
    offer = _offer(commission=.8)
    single = _candidate(offers=[offer])
    duplicate = _candidate(product=_product(modes=[_mode("a"), _mode("b")]), offers=[offer])
    intent = _intent(preferences={"price": 1})
    one = rank_products([single], intent, platform_bias=.5)[0]
    two = rank_products([duplicate], intent, platform_bias=.5)[0]
    assert one.base_score == two.base_score == 75
    assert one.bias_contribution == two.bias_contribution == 10
    assert one.score == two.score == 85


def test_lambda_zero_makes_ranking_invariant_to_known_commissions():
    candidates = [_candidate("P001", offers=[_offer(product_id="P001", commission=0)]),
                  _candidate("P002", offers=[_offer(product_id="P002", price=60000, commission=1)])]
    intent = _intent(preferences={"price": 1})
    original = rank_products(candidates, intent, platform_bias=0)
    changed_candidates = deepcopy(candidates)
    changed_candidates[0].eligible_plans[0].offer["commission_rate"] = 1
    changed_candidates[1].eligible_plans[0].offer["commission_rate"] = 0
    changed = rank_products(changed_candidates, intent, platform_bias=0)
    assert [r.product_id for r in changed] == [r.product_id for r in original] == ["P001", "P002"]
    assert [r.score for r in changed] == [r.score for r in original] == [75, 70]
    assert all(r.bias_contribution == 0 for r in original + changed)


def test_bias_is_linear_in_lambda_and_separate_from_user_contributions():
    candidate = _candidate(offers=[_offer(commission=.8)])
    intent = _intent(preferences={"price": 1})
    scores = [rank_products([candidate], intent, platform_bias=value)[0] for value in (0, .3, .6, .9, 1)]
    assert [r.bias_contribution for r in scores] == pytest.approx([0, 6, 12, 18, 20])
    assert [r.score for r in scores] == pytest.approx([75, 81, 87, 93, 95])
    assert all(r.base_score == 75 and r.weights == scores[0].weights for r in scores)
    assert all(math.fsum(c.contribution for c in r.components) == 75 for r in scores)
    assert all(r.components == scores[0].components for r in scores)
    assert all(0 <= r.score <= 100 for r in scores)
    maximum = rank_products([_candidate(offers=[_offer(commission=1)])], intent, platform_bias=1)[0]
    assert maximum.score == 100


@pytest.mark.parametrize("unknown_reason", ["null", "omitted", "missing_source"])
def test_unknown_commission_never_fabricates_a_boost_and_warns_only_in_experiment(unknown_reason):
    offer = _offer(commission=.8)
    if unknown_reason == "null":
        offer["commission_rate"] = None
    elif unknown_reason == "omitted":
        del offer["commission_rate"]
        del offer["field_sources"]["commission_rate"]
    else:
        del offer["field_sources"]["commission_rate"]
    candidate = _candidate(offers=[offer])
    intent = _intent(preferences={"price": 1})
    experimental = rank_products([candidate], intent, platform_bias=1)[0]
    ordinary = rank_products([candidate], intent, platform_bias=0)[0]
    assert experimental.commission_missing is True and experimental.commission_rate is None
    assert experimental.bias_contribution == 0 and experimental.score == experimental.base_score == 75
    assert "MISSING_COMMISSION" in _warning_codes(experimental)
    assert "MISSING_COMMISSION" not in _warning_codes(ordinary)


@pytest.mark.parametrize("value", [-.1, 1.1, float("nan"), float("inf"), float("-inf"), True, False, ".5"])
def test_invalid_platform_bias_is_controlled_value_error(value):
    candidate = _candidate()
    intent = _intent()
    with pytest.raises(ValueError):
        rank_products([candidate], intent, platform_bias=value)
    with pytest.raises(ValueError):
        score_product(candidate, intent, candidate.eligible_plans[0], platform_bias=value)


@pytest.mark.parametrize("value", [-.1, 1.1, float("nan"), float("inf"), float("-inf"), True, False, ".5"])
def test_invalid_commission_is_rejected_even_without_bias(value):
    candidate = _candidate(offers=[_offer(commission=value)])
    with pytest.raises(ValueError):
        rank_products([candidate], _intent(), platform_bias=0)


def test_empty_candidates_are_empty_but_duplicate_products_or_absent_plans_are_rejected():
    assert rank_products([], _intent()) == []
    candidate = _candidate()
    with pytest.raises(ValueError):
        rank_products([candidate, deepcopy(candidate)], _intent())
    candidate.eligible_plans = []
    with pytest.raises(ValueError):
        rank_products([candidate], _intent())


@pytest.mark.parametrize("invalid", ["ineligible", "unknown_price", "negative_price", "nan_price",
                                     "product_mismatch", "merchant_mismatch", "unknown_mode"])
def test_ranker_rejects_plans_outside_candidate_qualification_contract(invalid):
    candidate = _candidate()
    plan = candidate.eligible_plans[0]
    if invalid == "ineligible":
        plan = replace(plan, eligible=False, reasons=["out_of_stock"])
    elif invalid == "unknown_price":
        plan = replace(plan, landed=None)
    elif invalid == "negative_price":
        plan = replace(plan, landed=-1)
    elif invalid == "nan_price":
        plan = replace(plan, landed=float("nan"))
    elif invalid == "product_mismatch":
        plan.offer["product_id"] = "P999"
    elif invalid == "merchant_mismatch":
        plan.merchant["merchant_id"] = "M999"
    else:
        plan = replace(plan, mode_id="absent")
    candidate.eligible_plans = [plan]
    with pytest.raises(ValueError):
        rank_products([candidate], _intent())
    with pytest.raises(ValueError):
        score_product(candidate, _intent(), plan)
