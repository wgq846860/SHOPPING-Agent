"""S1.2 验收测试：校验器接受合法数据、拒绝全部负面样本。

负面样本分两类：
- tests/fixtures/invalid/*.json：单记录缺陷（从真实锚点记录派生）；
- 代码内突变：跨实体问题（重复身份、孤立报价、缺指标、数量对不上等）
  以及验收整改（F3–F6）新增的反例。
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.commerce_schema import build_field_sources
from scripts.validate_demo_data import validate_data_dir, validate_payload_record

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "invalid"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_valid_small_data_passes(small_data_dir):
    assert validate_data_dir(small_data_dir) == []


def test_missing_directory_reports_error(tmp_path):
    errors = validate_data_dir(tmp_path / "nope")
    assert errors and "不存在" in errors[0]


def _load_fixture(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _fixtures():
    return sorted(FIXTURE_DIR.glob("*.json"))


def test_negative_fixtures_exist():
    assert len(_fixtures()) >= 15


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_negative_fixture_rejected(path):
    case = _load_fixture(path)
    errors = validate_payload_record(
        case["entity_type"], case["payload"], case["data_version"])
    assert errors, f"{path.name} 应被拒绝"
    assert case["expect"] in " ".join(errors), (path.name, errors)


# ---- 跨实体突变（目录级） ----------------------------------------------------


def _copy_data(small_data_dir, tmp_path, name="mutated"):
    target = tmp_path / name
    target.mkdir()
    for fname in ("manifest.json", "merchants.json", "products.json",
                  "offers.json", "product_metrics.json"):
        (target / fname).write_text((small_data_dir / fname).read_text("utf-8"),
                                    encoding="utf-8")
    return target


def _rewrite(data_dir, name, items):
    wrapper = json.loads((data_dir / name).read_text("utf-8"))
    wrapper["items"] = items
    (data_dir / name).write_text(
        json.dumps(wrapper, ensure_ascii=False, indent=2) + "\n", "utf-8")


def _rewrite_manifest(data_dir, **updates):
    manifest = json.loads((data_dir / "manifest.json").read_text("utf-8"))
    manifest.update(updates)
    (data_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")


def test_duplicate_identity_key_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    products = json.loads((data / "products.json").read_text("utf-8"))["items"]
    products[1]["identity_key"] = products[0]["identity_key"]
    _rewrite(data, "products.json", products)
    errors = validate_data_dir(data)
    assert any("identity_key 重复" in e for e in errors)


def test_orphan_offer_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    offers = json.loads((data / "offers.json").read_text("utf-8"))["items"]
    offers[0]["merchant_id"] = "M999"
    _rewrite(data, "offers.json", offers)
    errors = validate_data_dir(data)
    assert any("不存在的商家" in e for e in errors)


def test_missing_metrics_row_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    metrics = json.loads((data / "product_metrics.json").read_text("utf-8"))["items"]
    removed = metrics.pop()
    _rewrite(data, "product_metrics.json", metrics)
    _rewrite_manifest(data, counts={"merchants": 5, "products": 15, "offers": 42,
                                    "product_metrics": 14})
    errors = validate_data_dir(data)
    assert any(f"{removed['product_id']}: 缺少 ProductMetrics" in e for e in errors)


def test_counts_mismatch_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    _rewrite_manifest(data, counts={"merchants": 99, "products": 15,
                                    "offers": 42, "product_metrics": 15})
    errors = validate_data_dir(data)
    assert any("与实际 5 不符" in e for e in errors)


def test_duplicate_product_merchant_offer_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    offers = json.loads((data / "offers.json").read_text("utf-8"))["items"]
    clone = dict(offers[0])
    clone["offer_id"] = "O999"
    offers.append(clone)
    _rewrite(data, "offers.json", offers)
    _rewrite_manifest(data, counts={"merchants": 5, "products": 15,
                                    "offers": 43, "product_metrics": 15})
    errors = validate_data_dir(data)
    assert any("(product,merchant) 报价重复" in e for e in errors)


def test_unknown_accessory_code_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    offers = json.loads((data / "offers.json").read_text("utf-8"))["items"]
    victim = next(o for o in offers if o["offer_id"] == "O001")
    victim["accessories"] = [{
        "accessory_code": "NO-SUCH-CODE", "included": True, "price_cents": 0,
        "stock": None, "delivery_in_same_package": True}]
    _rewrite(data, "offers.json", offers)
    errors = validate_data_dir(data)
    assert any("不是该商品的必要配件" in e for e in errors)


def test_merchant_without_offer_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    offers = [o for o in json.loads((data / "offers.json").read_text("utf-8"))["items"]
              if o["merchant_id"] != "M005"]
    _rewrite(data, "offers.json", offers)
    _rewrite_manifest(data, counts={"merchants": 5, "products": 15,
                                    "offers": len(offers), "product_metrics": 15})
    errors = validate_data_dir(data)
    assert any("M005: 商家没有任何报价关联" in e for e in errors)


def test_nan_rejected(small_data_dir, tmp_path):
    data = _copy_data(small_data_dir, tmp_path)
    text = (data / "products.json").read_text("utf-8").replace(
        '"hours": 20', '"hours": NaN', 1)
    (data / "products.json").write_text(text, "utf-8")
    errors = validate_data_dir(data)
    assert any("不允许" in e for e in errors)


# ---- F4 回归：结构 ID 唯一性、测量引用、counts、逐商品报价数 ----------------


def _first_product(small_data_dir):
    items = json.loads((small_data_dir / "products.json").read_text("utf-8"))["items"]
    return copy.deepcopy(items[0])


def test_duplicate_mode_id_rejected(small_data_dir, tmp_path):
    """F4：connection_modes.mode_id 商品内必须唯一（即使来源覆盖已补齐）。"""
    p = _first_product(small_data_dir)
    p["connection_modes"].append(copy.deepcopy(p["connection_modes"][0]))
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")
    errors = validate_payload_record("product", p, "small-v1")
    assert any("connection_modes.mode_id" in e and "重复" in e for e in errors)


def test_duplicate_battery_spec_id_rejected(small_data_dir):
    p = _first_product(small_data_dir)
    p["battery_specs"].append(copy.deepcopy(p["battery_specs"][0]))
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")
    errors = validate_payload_record("product", p, "small-v1")
    assert any("battery_specs.battery_spec_id" in e and "重复" in e for e in errors)


def test_measurement_unknown_mode_rejected_at_dir_level(small_data_dir, tmp_path):
    """F4：测量 mode_id 引用同商品不存在的模式 → 目录级拒绝（跨实体引用）。"""
    data = _copy_data(small_data_dir, tmp_path)
    metrics = json.loads((data / "product_metrics.json").read_text("utf-8"))["items"]
    victim = next(m for m in metrics if m.get("measurements"))
    victim["measurements"][0]["mode_id"] = "nonexistent_mode"
    _rewrite(data, "product_metrics.json", metrics)
    errors = validate_data_dir(data)
    assert any("measurement" in e and "未在商品 connection_modes 中定义" in e
               for e in errors)


def test_manifest_counts_missing_or_bad_type_rejected(small_data_dir, tmp_path):
    """F4：counts 必须含完整键集合且为非负整数（{} 不得静默通过）。"""
    data = _copy_data(small_data_dir, tmp_path)
    manifest = json.loads((data / "manifest.json").read_text("utf-8"))
    manifest["counts"] = {}
    (data / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    errors = validate_data_dir(data)
    assert any("counts 缺少键" in e for e in errors)

    data2 = _copy_data(small_data_dir, tmp_path, name="mutated-bad-type")
    manifest2 = json.loads((data2 / "manifest.json").read_text("utf-8"))
    manifest2["counts"]["offers"] = "42"  # 字符串冒充整数
    (data2 / "manifest.json").write_text(
        json.dumps(manifest2, ensure_ascii=False, indent=2) + "\n", "utf-8")
    errors2 = validate_data_dir(data2)
    assert any("counts.offers 必须是非负整数" in e for e in errors2)


def test_product_without_any_offer_rejected(small_data_dir, tmp_path):
    """F4：零报价商品必须被拒绝（不能只遍历"已有报价的商品"）。"""
    data = _copy_data(small_data_dir, tmp_path)
    offers = [o for o in json.loads((data / "offers.json").read_text("utf-8"))["items"]
              if o["product_id"] != "P015"]
    _rewrite(data, "offers.json", offers)
    manifest = json.loads((data / "manifest.json").read_text("utf-8"))
    manifest["counts"]["offers"] = len(offers)
    (data / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")
    errors = validate_data_dir(data)
    assert any("P015: 商品没有任何报价关联" in e for e in errors)


# ---- F5 回归：时间先后关系（可注入参考时间） --------------------------------


def test_updated_at_before_observed_rejected(small_data_dir):
    """F5：updated_at 早于来源 observed_at → 拒绝（参考时间可控）。"""
    p = _first_product(small_data_dir)
    reference = datetime(2026, 9, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    assert validate_payload_record("product", p, "small-v1", now=reference) == []
    p["updated_at"] = "2026-09-28T10:00:00+08:00"  # 早于来源的 2026-09-29
    errors = validate_payload_record("product", p, "small-v1", now=reference)
    assert any("早于来源观测时间" in e for e in errors)


def test_future_observed_time_rejected(small_data_dir):
    """F5：观测时间在未来（2099）→ 拒绝（默认参考时间=当前时刻）。"""
    p = _first_product(small_data_dir)
    for source in p["field_sources"].values():
        source["observed_at"] = "2099-01-01T10:00:00+08:00"
    p["updated_at"] = "2099-01-01T10:00:00+08:00"
    errors = validate_payload_record("product", p, "small-v1")
    assert any("不得为未来" in e for e in errors)


def test_future_observation_strict_boundaries(small_data_dir):
    """R1：严格未来检查——等于参考时间通过，晚于参考时间（哪怕2小时）拒绝。

    取代原"未来时间在24小时容差内应通过"的测试（R1 判定该容差未经约定、
    放宽了字典契约）。
    """
    p = _first_product(small_data_dir)
    reference = datetime(2026, 9, 30, 12, 0, tzinfo=timezone(timedelta(hours=8)))

    def with_timestamp(offset: timedelta):
        q = copy.deepcopy(p)
        ts = (reference + offset).isoformat()
        q["updated_at"] = ts
        for source in q["field_sources"].values():
            source["observed_at"] = ts
        return q

    # 等于参考时间：通过
    assert validate_payload_record(
        "product", with_timestamp(timedelta(0)), "small-v1", now=reference) == []
    # 早于参考时间：通过
    assert validate_payload_record(
        "product", with_timestamp(timedelta(hours=-2)), "small-v1", now=reference) == []
    # 晚于参考时间 2 小时：必须拒绝（无任何容差）
    errors_2h = validate_payload_record(
        "product", with_timestamp(timedelta(hours=2)), "small-v1", now=reference)
    assert any("在参考时间之后" in e for e in errors_2h)
    # 晚于参考时间 24 小时：拒绝
    errors_24h = validate_payload_record(
        "product", with_timestamp(timedelta(hours=24)), "small-v1", now=reference)
    assert any("在参考时间之后" in e for e in errors_24h)


# ---- F6 回归：畸形结构返回受控错误而非异常 ----------------------------------


@pytest.mark.parametrize("field,value,entity_kind", [
    ("connection_modes", "not-an-array", "product"),
    ("battery_specs", 12345, "product"),
    ("compatibility", {"oops": True}, "product"),
    ("accessories", "oops", "offer"),
    ("delivery", "oops", "offer"),
    ("measurements", "oops", "product_metrics"),
])
def test_malformed_structures_return_controlled_errors(
        small_data_dir, field, value, entity_kind):
    """F6：形状非法时不得抛 AttributeError，必须返回带字段路径的错误列表。"""
    if entity_kind == "product":
        payload = _first_product(small_data_dir)
    elif entity_kind == "offer":
        offers = json.loads((small_data_dir / "offers.json").read_text("utf-8"))["items"]
        payload = copy.deepcopy(offers[0])
    else:
        metrics = json.loads(
            (small_data_dir / "product_metrics.json").read_text("utf-8"))["items"]
        payload = copy.deepcopy(next(m for m in metrics if m.get("measurements")))
    payload[field] = value
    errors = validate_payload_record(entity_kind, payload, "small-v1")
    assert isinstance(errors, list) and errors
    assert any(field in e for e in errors), (field, errors[:3])


def test_non_dict_record_rejected(small_data_dir):
    errors = validate_payload_record("product", ["not", "a", "dict"], "small-v1")
    assert errors == ["记录必须是对象，实际为 list"]


# ---- F3 回归：同条件 ANC 开/关续航一致性 -------------------------------------


def test_anc_on_hours_must_not_exceed_off(small_data_dir):
    """F3：同模式同口径下 ANC 开启续航长于关闭续航 → 拒绝。"""
    p = _first_product(small_data_dir)  # P001：只有 ANC 开启规格，先补一条关闭规格
    mode_id = p["connection_modes"][0]["mode_id"]
    p["battery_specs"].append({
        "battery_spec_id": "B2", "hours": 15, "basis": "single_charge",
        "anc_state": "off", "mode_id": mode_id, "test_conditions": "模拟50%音量SBC听音"})
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")

    # P001 开启 20h > 关闭 15h，应被拒绝
    errors = validate_payload_record("product", p, "small-v1")
    assert any("ANC 开启续航" in e and "不得长于关闭续航" in e for e in errors)

    p["battery_specs"][0]["hours"] = 12  # 开 12h ≤ 关 15h → 合法
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")
    assert validate_payload_record("product", p, "small-v1") == []


def test_battery_different_conditions_not_compared(small_data_dir):
    """R2：不同测试条件不得仅因 20>15 被判矛盾（50%音量开20h vs 90%音量关15h）。"""
    p = _first_product(small_data_dir)
    mode_id = p["connection_modes"][0]["mode_id"]
    assert p["battery_specs"][0]["test_conditions"] == "模拟50%音量SBC听音"
    p["battery_specs"].append({
        "battery_spec_id": "B2", "hours": 15, "basis": "single_charge",
        "anc_state": "off", "mode_id": mode_id,
        "test_conditions": "模拟90%音量SBC听音"})
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")
    errors = validate_payload_record("product", p, "small-v1")
    assert not any("ANC 开启续航" in e for e in errors), errors


def test_battery_missing_conditions_not_compared(small_data_dir):
    """R2：条件缺失（null）时不可比——不得假定相同而误判矛盾。"""
    p = _first_product(small_data_dir)
    mode_id = p["connection_modes"][0]["mode_id"]
    p["battery_specs"].append({
        "battery_spec_id": "B2", "hours": 15, "basis": "single_charge",
        "anc_state": "off", "mode_id": mode_id, "test_conditions": None})
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")
    errors = validate_payload_record("product", p, "small-v1")
    assert not any("ANC 开启续航" in e for e in errors), errors


def test_battery_same_conditions_extremes_still_rejected(small_data_dir):
    """R2：同条件下多条记录仍取极值比较（不会因分组变细而漏检）。"""
    p = _first_product(small_data_dir)
    mode_id = p["connection_modes"][0]["mode_id"]
    conditions = "模拟50%音量SBC听音"
    p["battery_specs"].append({
        "battery_spec_id": "B2", "hours": 15, "basis": "single_charge",
        "anc_state": "off", "mode_id": mode_id, "test_conditions": conditions})
    p["battery_specs"].append({
        "battery_spec_id": "B3", "hours": 25, "basis": "single_charge",
        "anc_state": "on", "mode_id": mode_id, "test_conditions": conditions})
    p["field_sources"] = build_field_sources(
        p, "product", data_version="small-v1",
        observed_at="2026-09-29T10:00:00+08:00", method="回归测试突变")
    errors = validate_payload_record("product", p, "small-v1")
    assert any("ANC 开启续航" in e and "不得长于关闭续航" in e for e in errors)


# ---- R3 回归：畸形输入必须返回受控校验错误（含目录级与 CLI） ----------------


def test_null_delivery_returns_controlled_errors(small_data_dir):
    """R3：Offer.delivery=null → 必填错误，而非 AttributeError。"""
    offers = json.loads((small_data_dir / "offers.json").read_text("utf-8"))["items"]
    offer = copy.deepcopy(offers[0])
    offer["delivery"] = None
    errors = validate_payload_record("offer", offer, "small-v1")
    assert isinstance(errors, list) and errors
    assert any("delivery" in e and ("必填" in e or "不能为 null" in e) for e in errors)


def test_non_object_directory_record_rejected(small_data_dir, tmp_path):
    """R3：products.json items 混入整数 42 → 目录校验返回带文件与序号的错误，
    不抛 AttributeError，跨实体检查安全跳过。"""
    data = _copy_data(small_data_dir, tmp_path, name="non-object")
    products = json.loads((data / "products.json").read_text("utf-8"))
    products["items"][0] = 42
    (data / "products.json").write_text(
        json.dumps(products, ensure_ascii=False, indent=2) + "\n", "utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert any("products.json[0]" in e and "必须是对象" in e for e in errors)
    assert not any("Traceback" in e for e in errors)


def test_cli_reports_broken_dir_without_traceback(small_data_dir, tmp_path):
    """R3：CLI 对畸形目录必须以退出码 1 + FAIL 报告，不得打印未处理异常。"""
    data = _copy_data(small_data_dir, tmp_path, name="cli-broken")
    offers = json.loads((data / "offers.json").read_text("utf-8"))
    offers["items"][0]["delivery"] = None
    (data / "offers.json").write_text(
        json.dumps(offers, ensure_ascii=False, indent=2) + "\n", "utf-8")
    products = json.loads((data / "products.json").read_text("utf-8"))
    products["items"][0] = 42
    (data / "products.json").write_text(
        json.dumps(products, ensure_ascii=False, indent=2) + "\n", "utf-8")

    script = PROJECT_ROOT / "scripts" / "validate_demo_data.py"
    result = subprocess.run(
        [sys.executable, str(script), "--data-dir", str(data)],
        capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL" in result.stdout
    assert "Traceback" not in result.stderr
    assert "AttributeError" not in result.stderr


# ---- REVIEW-S1-20260930 F1 回归：形状错误后一致性检查不得崩溃 ----------------
#
# 审查报告的反例：形状检查已报告类型/缺失错误后，一致性检查仍对非法值
# 规范化/比较/入集合，导致 TypeError/KeyError 而非受控错误列表。


def _first_record(small_data_dir, name, index=0):
    items = json.loads((small_data_dir / name).read_text("utf-8"))["items"]
    return copy.deepcopy(items[index])


def _mutate_price_to_string(rec):
    rec["price_cents"] = "oops"


def _delete_price(rec):
    del rec["price_cents"]


def _mutate_brand_to_int(rec):
    rec["brand"] = 42


def _mutate_mode_id_to_list(rec):
    rec["connection_modes"][0]["mode_id"] = []


def _mutate_battery_mode_id_to_list(rec):
    rec["battery_specs"][0]["mode_id"] = []


@pytest.mark.parametrize(
    "name,kind,mutate,expect",
    [
        ("offers.json", "offer", _mutate_price_to_string, "price_cents"),
        ("offers.json", "offer", _delete_price, "price_cents"),
        ("products.json", "product", _mutate_brand_to_int, "brand"),
        ("products.json", "product", _mutate_mode_id_to_list, "mode_id"),
        ("products.json", "product", _mutate_battery_mode_id_to_list, "mode_id"),
    ],
    ids=["price-str", "price-missing", "brand-int", "mode-id-list", "battery-mode-id-list"],
)
def test_shape_invalid_record_returns_errors_not_crash(
        small_data_dir, name, kind, mutate, expect):
    """F1：单记录接口对非法值返回错误列表，不抛 TypeError/KeyError。"""
    payload = _first_record(small_data_dir, name)
    mutate(payload)
    errors = validate_payload_record(kind, payload, "small-v1")
    assert isinstance(errors, list) and errors, "形状非法必须返回非空错误列表"
    assert any(expect in e for e in errors), (expect, errors[:5])


@pytest.mark.parametrize("raw,expected_type", [("null", "NoneType"), ("42", "int"), ("[]", "list")])
def test_manifest_non_object_returns_controlled_error(
        small_data_dir, tmp_path, raw, expected_type):
    """F1：manifest.json 非对象（null/42/[]）→ 受控错误，不 set() 崩溃。"""
    data = _copy_data(small_data_dir, tmp_path, name=f"manifest-{expected_type}")
    (data / "manifest.json").write_text(raw, encoding="utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert "manifest" in errors[0] and expected_type in errors[0], errors


def test_wrapper_non_object_returns_controlled_error(small_data_dir, tmp_path):
    """F1 验收补充：实体文件包装对象为 null → 受控错误，不 set(wrapper) 崩溃。"""
    data = _copy_data(small_data_dir, tmp_path, name="wrapper-null")
    (data / "offers.json").write_text("null", encoding="utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert any("offers.json" in e and "对象" in e for e in errors)


def test_directory_invalid_entity_ids_controlled(small_data_dir, tmp_path):
    """F1 验收：ID=[]（商品）/ID={}（商家）→ 形状错误 + 跨实体检查安全跳过。"""
    data = _copy_data(small_data_dir, tmp_path, name="bad-ids")
    products = json.loads((data / "products.json").read_text("utf-8"))
    products["items"][0]["product_id"] = []
    merchants = json.loads((data / "merchants.json").read_text("utf-8"))
    merchants["items"][0]["merchant_id"] = {}
    (data / "products.json").write_text(
        json.dumps(products, ensure_ascii=False), "utf-8")
    (data / "merchants.json").write_text(
        json.dumps(merchants, ensure_ascii=False), "utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert any("product_id" in e for e in errors)
    assert any("merchant_id" in e for e in errors)


def test_directory_missing_price_controlled(small_data_dir, tmp_path):
    """F1 验收：目录级必需金额缺失（del price_cents）→ 受控错误，无 KeyError。"""
    data = _copy_data(small_data_dir, tmp_path, name="missing-price")
    offers = json.loads((data / "offers.json").read_text("utf-8"))
    del offers["items"][0]["price_cents"]
    (data / "offers.json").write_text(
        json.dumps(offers, ensure_ascii=False), "utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert any("price_cents" in e and "缺少字段" in e for e in errors)


def test_cli_shape_errors_no_traceback(small_data_dir, tmp_path):
    """F1：CLI 对形状非法数据退出 1 + FAIL，不打印 Traceback/异常名。"""
    data = _copy_data(small_data_dir, tmp_path, name="cli-shape")
    offers = json.loads((data / "offers.json").read_text("utf-8"))
    offers["items"][0]["price_cents"] = "oops"
    (data / "offers.json").write_text(
        json.dumps(offers, ensure_ascii=False), "utf-8")
    script = PROJECT_ROOT / "scripts" / "validate_demo_data.py"
    result = subprocess.run(
        [sys.executable, str(script), "--data-dir", str(data)],
        capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL" in result.stdout
    for banned in ("Traceback", "TypeError", "KeyError"):
        assert banned not in result.stderr, banned


def test_failed_import_preserves_original_db(small_data_dir, tmp_path):
    """F1 验收：畸形批次在导入前被校验拒绝，原库行数与 manifest 保持不变。"""
    from backend.database.db import (
        connect,
        count_rows,
        get_manifest,
        initialize_database,
    )

    db_path = tmp_path / "db" / "shopping.sqlite3"
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    try:
        rows_before = count_rows(conn)
        manifest_before = get_manifest(conn)
    finally:
        conn.close()

    broken = _copy_data(small_data_dir, tmp_path, name="broken-batch")
    offers = json.loads((broken / "offers.json").read_text("utf-8"))
    offers["items"][0]["price_cents"] = "oops"
    (broken / "offers.json").write_text(
        json.dumps(offers, ensure_ascii=False), "utf-8")

    with pytest.raises(ValueError, match="数据校验未通过"):
        initialize_database(db_path, broken)

    conn = connect(db_path)
    try:
        assert count_rows(conn) == rows_before
        assert get_manifest(conn) == manifest_before
    finally:
        conn.close()


# ---- R1 回归（s1_f1_f4_reaudit_20260930）：枚举/配件/测量畸形输入受控拒绝 ----
#
# F1 整改后仍遗漏的五类反例：枚举值、required_accessory_codes、
# accessory_code、Measurement.metric 的非法类型在 set/dict 访问处崩溃。


def _first_metrics_with_measurements(small_data_dir):
    items = json.loads((small_data_dir / "product_metrics.json").read_text("utf-8"))["items"]
    return copy.deepcopy(next(m for m in items if m.get("measurements")))


def _first_offer_with_accessories(small_data_dir):
    items = json.loads((small_data_dir / "offers.json").read_text("utf-8"))["items"]
    return copy.deepcopy(next(o for o in items if o.get("accessories")))


def _mutate_sub_category_list(rec):
    rec["sub_category"] = []


def _mutate_required_codes_int(rec):
    rec["connection_modes"][0]["required_accessory_codes"] = 42


def _mutate_required_codes_nested(rec):
    rec["connection_modes"][0]["required_accessory_codes"] = [[]]


def _mutate_metric_list(rec):
    rec["measurements"][0]["metric"] = []


def _mutate_accessory_code_list(rec):
    rec["accessories"][0]["accessory_code"] = []


R1_CASES = [
    ("products.json", "product", _mutate_sub_category_list, "sub_category"),
    ("products.json", "product", _mutate_required_codes_int, "required_accessory_codes"),
    ("products.json", "product", _mutate_required_codes_nested, "required_accessory_codes"),
    ("product_metrics.json", "product_metrics", _mutate_metric_list, "metric"),
    ("offers.json", "offer", _mutate_accessory_code_list, "accessory_code"),
]
R1_IDS = ["enum-list", "codes-int", "codes-nested", "metric-list", "acc-code-list"]


def _r1_record(small_data_dir, kind):
    if kind == "product":
        return _first_product(small_data_dir)
    if kind == "offer":
        return _first_offer_with_accessories(small_data_dir)
    return _first_metrics_with_measurements(small_data_dir)


@pytest.mark.parametrize("name,kind,mutate,expect", R1_CASES, ids=R1_IDS)
def test_r1_single_record_controlled(small_data_dir, name, kind, mutate, expect):
    """R1：单记录校验对枚举/关联字段的非法类型返回错误列表，不抛 TypeError。"""
    payload = _r1_record(small_data_dir, kind)
    mutate(payload)
    errors = validate_payload_record(kind, payload, "small-v1")
    assert isinstance(errors, list) and errors, "畸形输入必须返回非空错误列表"
    assert any(expect in e for e in errors), (expect, errors[:5])


@pytest.mark.parametrize("name,kind,mutate,expect", R1_CASES, ids=R1_IDS)
def test_r1_directory_controlled(small_data_dir, tmp_path, name, kind, mutate, expect):
    """R1：目录级校验（含跨实体配件关联）对畸形输入受控拒绝。"""
    data = _copy_data(small_data_dir, tmp_path, name=f"r1-dir-{mutate.__name__}")
    wrapper = json.loads((data / name).read_text("utf-8"))
    if kind == "product":
        index = 0
    elif kind == "offer":
        index = next(i for i, o in enumerate(wrapper["items"]) if o.get("accessories"))
    else:
        index = next(i for i, m in enumerate(wrapper["items"]) if m.get("measurements"))
    mutate(wrapper["items"][index])
    (data / name).write_text(json.dumps(wrapper, ensure_ascii=False), "utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert any(expect in e for e in errors), (expect, errors[:6])


@pytest.mark.parametrize("name,kind,mutate,expect", R1_CASES, ids=R1_IDS)
def test_r1_cli_no_traceback(small_data_dir, tmp_path, name, kind, mutate, expect):
    """R1：CLI 对五类畸形输入退出 1 + FAIL，不打印 Traceback/异常名。"""
    data = _copy_data(small_data_dir, tmp_path, name=f"r1-cli-{mutate.__name__}")
    wrapper = json.loads((data / name).read_text("utf-8"))
    if kind == "product":
        index = 0
    elif kind == "offer":
        index = next(i for i, o in enumerate(wrapper["items"]) if o.get("accessories"))
    else:
        index = next(i for i, m in enumerate(wrapper["items"]) if m.get("measurements"))
    mutate(wrapper["items"][index])
    (data / name).write_text(json.dumps(wrapper, ensure_ascii=False), "utf-8")

    script = PROJECT_ROOT / "scripts" / "validate_demo_data.py"
    result = subprocess.run(
        [sys.executable, str(script), "--data-dir", str(data)],
        capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert "FAIL" in result.stdout
    for banned in ("Traceback", "TypeError", "KeyError"):
        assert banned not in result.stderr, banned


# ---- N1/N2 回归（s1_overall_review_20260930）：日期语义与可计算范围入口校验 ----
#
# N1：valid_until 只查字符串 → 非法/无时区日期入库后查询端 parse_iso 抛错；
# N2：配送天数只查非负与 min≤max → 极大值在日期加法处溢出。


def _offer_by_id(small_data_dir, offer_id="O004"):
    items = json.loads((small_data_dir / "offers.json").read_text("utf-8"))["items"]
    return copy.deepcopy(next(o for o in items if o["offer_id"] == offer_id))


def _set_valid_until(rec, value, reason="not_collected"):
    rec["valid_until"] = value
    rec["field_sources"]["valid_until"]["missing_reason"] = (
        None if value is not None else reason)


def _set_delivery_days(rec, days):
    rec["delivery"]["min_days"] = days
    rec["delivery"]["max_days"] = days


@pytest.mark.parametrize("value", ["not-a-date", "2030-01-01T00:00:00"])
def test_n1_invalid_valid_until_rejected(small_data_dir, value):
    """N1：非法字符串/无时区 valid_until → 单记录受控拒绝（字段定位）。"""
    offer = _offer_by_id(small_data_dir)
    _set_valid_until(offer, value)
    errors = validate_payload_record("offer", offer, "small-v1")
    assert isinstance(errors, list) and errors
    assert any("valid_until" in e for e in errors), (value, errors[:5])


def test_n1_directory_invalid_valid_until_rejected(small_data_dir, tmp_path):
    """N1：目录层同样拒绝非法/无时区 valid_until。"""
    for value in ("not-a-date", "2030-01-01T00:00:00"):
        data = _copy_data(small_data_dir, tmp_path, name=f"n1-{value[:4]}")
        offers = json.loads((data / "offers.json").read_text("utf-8"))
        target = next(o for o in offers["items"] if o["offer_id"] == "O004")
        _set_valid_until(target, value)
        (data / "offers.json").write_text(
            json.dumps(offers, ensure_ascii=False), "utf-8")
        errors = validate_data_dir(data)
        assert isinstance(errors, list) and errors, value
        assert any("valid_until" in e for e in errors), (value, errors[:5])


def test_n1_legal_and_null_valid_until_pass(small_data_dir):
    """N1 对照：带时区合法有效期与 null（未给截止）都不判为格式错误。"""
    offer = _offer_by_id(small_data_dir)
    _set_valid_until(offer, "2030-01-01T00:00:00+08:00")
    assert validate_payload_record("offer", offer, "small-v1") == []
    _set_valid_until(offer, None)
    assert validate_payload_record("offer", offer, "small-v1") == []


def test_n1_expired_with_timezone_is_not_format_error(small_data_dir):
    """N1 对照：合法但已过期（如 O001 的 2026-09-29+08:00）不是格式错误。"""
    o001 = _offer_by_id(small_data_dir, "O001")
    assert o001["valid_until"] == "2026-09-29T23:59:59+08:00" or o001["valid_until"]
    errors = validate_payload_record("offer", o001, "small-v1")
    assert not any("valid_until" in e for e in errors), errors


def test_n2_huge_delivery_days_rejected(small_data_dir):
    """N2：10^9 配送天数在观测日期上不可计算 → 单记录受控拒绝。"""
    offer = _offer_by_id(small_data_dir)
    _set_delivery_days(offer, 1000000000)
    errors = validate_payload_record("offer", offer, "small-v1")
    assert isinstance(errors, list) and errors
    assert any("delivery.min_days" in e and "溢出" in e for e in errors), errors[:5]
    assert any("delivery.max_days" in e and "溢出" in e for e in errors), errors[:5]


def test_n2_directory_huge_delivery_days_rejected(small_data_dir, tmp_path):
    """N2：目录层同样拒绝不可计算的配送天数。"""
    data = _copy_data(small_data_dir, tmp_path, name="n2-huge")
    offers = json.loads((data / "offers.json").read_text("utf-8"))
    target = next(o for o in offers["items"] if o["offer_id"] == "O004")
    _set_delivery_days(target, 1000000000)
    (data / "offers.json").write_text(
        json.dumps(offers, ensure_ascii=False), "utf-8")
    errors = validate_data_dir(data)
    assert isinstance(errors, list) and errors
    assert any("delivery." in e and "溢出" in e for e in errors), errors[:6]


def test_n2_computable_large_days_pass(small_data_dir):
    """N2 对照：可计算范围内的天数（约 200 万天，未超 date.max）不判错。"""
    offer = _offer_by_id(small_data_dir)
    _set_delivery_days(offer, 2000000)
    assert validate_payload_record("offer", offer, "small-v1") == []


def test_n1_n2_bad_batch_import_rejected(small_data_dir, tmp_path):
    """N1/N2：畸形有效期与超大配送天数批次在导入前被拒，不建库/不动原库。"""
    from backend.database.db import initialize_database

    for name, mutate in (
            ("n1-import", lambda o: _set_valid_until(o, "not-a-date")),
            ("n2-import", lambda o: _set_delivery_days(o, 1000000000))):
        data = _copy_data(small_data_dir, tmp_path, name=name)
        offers = json.loads((data / "offers.json").read_text("utf-8"))
        mutate(next(o for o in offers["items"] if o["offer_id"] == "O004"))
        (data / "offers.json").write_text(
            json.dumps(offers, ensure_ascii=False), "utf-8")
        db_path = tmp_path / f"{name}-db.sqlite3"
        with pytest.raises(ValueError, match="数据校验未通过"):
            initialize_database(db_path, data)
        assert not db_path.exists()
