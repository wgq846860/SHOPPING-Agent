"""S1.1 验收测试：生成器可复现、规模对账、取舍/异常锚点齐备。

锚点语义与 scripts/generate_demo_data.py 文件头注释一一对应；
可复现性按"实体文件逐字节一致、manifest 仅 generated_at 可不同"验收。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---- 工具 --------------------------------------------------------------------


def _load(data_dir: Path, name: str) -> list[dict]:
    wrapper = json.loads((data_dir / name).read_text(encoding="utf-8"))
    return wrapper["items"]


@pytest.fixture()
def small(small_data_dir):
    return {
        "dir": small_data_dir,
        "manifest": json.loads((small_data_dir / "manifest.json").read_text("utf-8")),
        "merchants": _load(small_data_dir, "merchants.json"),
        "products": _load(small_data_dir, "products.json"),
        "offers": _load(small_data_dir, "offers.json"),
        "metrics": _load(small_data_dir, "product_metrics.json"),
    }


def _offer(small, offer_id):
    return next(o for o in small["offers"] if o["offer_id"] == offer_id)


def _product(small, product_id):
    return next(p for p in small["products"] if p["product_id"] == product_id)


def _landed(o):
    """无未知分量时的到手价；必要配件未包含的按配件价合计。"""
    total = o["price_cents"] + (o["shipping_fee_cents"] or 0) \
        + sum(a["price_cents"] or 0 for a in (o["accessories"] or [])
              if a["included"] is False) - (o["instant_discount_cents"] or 0)
    return total


# ---- 可复现性与规模 ----------------------------------------------------------


def test_generation_is_byte_reproducible(tmp_path):
    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    generate_profile("small", dir_a, DEFAULT_SEED)
    generate_profile("small", dir_b, DEFAULT_SEED)
    for name in ("merchants.json", "products.json", "offers.json",
                 "product_metrics.json"):
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes(), name
    manifest_a = json.loads((dir_a / "manifest.json").read_text("utf-8"))
    manifest_b = json.loads((dir_b / "manifest.json").read_text("utf-8"))
    manifest_a.pop("generated_at")
    manifest_b.pop("generated_at")
    assert manifest_a == manifest_b


def test_generator_cli_runs_and_writes_data(tmp_path):
    """通过命令行运行生成器（README 记载的使用方式）。"""
    script = PROJECT_ROOT / "scripts" / "generate_demo_data.py"
    result = subprocess.run(
        [sys.executable, str(script), "--profile", "small", "--out", str(tmp_path)],
        capture_output=True, text=True, cwd=PROJECT_ROOT)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "manifest.json").is_file()


def test_small_profile_counts(small):
    assert small["manifest"]["counts"] == {
        "merchants": 5, "products": 15, "offers": 42, "product_metrics": 15}
    assert small["manifest"]["data_version"] == "small-v1"
    assert small["manifest"]["profile"] == "small"
    assert isinstance(small["manifest"]["seed"], int)
    assert len(small["merchants"]) == 5
    assert len(small["products"]) == 15
    assert len(small["offers"]) == 42
    assert len(small["metrics"]) == 15


def test_contract_example_records_aligned(small):
    """P001/O001/M001 与 api_contract.md §4/§5/§6 示例数值一致。"""
    p1 = _product(small, "P001")
    assert p1["identity_key"] == '["auraloom","metro1","black","rev1","cn"]'
    assert p1["brand"] == "Auraloom" and p1["product_name"] == "Metro One"
    assert p1["wireless"] is True and p1["active_noise_cancelling"] is True
    assert p1["battery_specs"][0]["hours"] == 20
    assert p1["battery_specs"][0]["basis"] == "single_charge"
    assert p1["battery_specs"][0]["anc_state"] == "on"
    assert p1["weight_specs"][0]["grams"] == 240
    assert p1["weight_specs"][0]["basis"] == "headset"

    o1 = _offer(small, "O001")
    assert o1["product_id"] == "P001" and o1["merchant_id"] == "M001"
    assert o1["price_cents"] == 69900
    assert o1["original_price_cents"] == 79900
    assert o1["shipping_fee_cents"] == 0
    assert o1["stock"] == 12
    assert o1["return_policy_override"]["days"] == 7
    assert o1["return_policy_override"]["opened_return_allowed"] is False
    assert o1["valid_until"] == "2026-09-29T23:59:59+08:00"

    m1 = next(m for m in small["merchants"] if m["merchant_id"] == "M001")
    assert m1["merchant_name"] == "AudioWorld"
    assert m1["merchant_rating"] == 4.7
    assert m1["service_score"] == 86 and m1["shipping_score"] == 90
    assert m1["default_return_policy"]["days"] == 30
    assert m1["default_warranty"]["months"] == 12


def test_six_sub_categories_covered(small):
    assert {p["sub_category"] for p in small["products"]} == {
        "over_ear_headphones", "on_ear_headphones", "true_wireless_earbuds",
        "wireless_earbuds", "gaming_headsets", "wired_headphones"}


def test_every_merchant_has_offers(small):
    counts = {m["merchant_id"]: 0 for m in small["merchants"]}
    for o in small["offers"]:
        counts[o["merchant_id"]] += 1
    assert all(v >= 1 for v in counts.values()), counts


# ---- 计划要求的取舍样例 ------------------------------------------------------


def test_tradeoff_cheap_but_slow_and_no_aftermarket(small):
    """O011：同商品中最便宜，但配送最慢、无退货、无保修。"""
    p3_offers = [o for o in small["offers"] if o["product_id"] == "P003"]
    cheapest = min(p3_offers, key=_landed)
    assert cheapest["offer_id"] == "O011"
    assert cheapest["delivery"]["max_days"] >= 8
    merchant = next(m for m in small["merchants"]
                    if m["merchant_id"] == cheapest["merchant_id"])
    assert merchant["default_return_policy"]["days"] == 0
    assert merchant["default_warranty"]["months"] == 0


def test_tradeoff_light_but_short_battery(small):
    """P014：单次续航 6 小时（有已知单次续航数据的产品中最短），单只 5.2g 轻量。"""
    p14 = _product(small, "P014")
    single = next(s for s in p14["battery_specs"] if s["basis"] == "single_charge")
    assert single["hours"] == 6
    known_singles = [s["hours"] for p in small["products"]
                     for s in (p["battery_specs"] or [])
                     if s["basis"] == "single_charge" and s["hours"] is not None]
    assert single["hours"] == min(known_singles)
    weights = {p["product_id"]: max(s["grams"] for s in p["weight_specs"])
               for p in small["products"]
               if p["weight_specs"] and p["weight_specs"][0]["basis"] == "single_earbud"}
    assert weights["P014"] < 6  # 轻量梯队（P005 4.8g 更轻，但单次续航未知）


def test_tradeoff_pricier_but_better_aftermarket(small):
    """M005 报价更贵但退货 30 天可开封试戴、保修 24 个月。"""
    m5 = next(m for m in small["merchants"] if m["merchant_id"] == "M005")
    assert m5["default_return_policy"]["days"] == 30
    assert m5["default_return_policy"]["opened_return_allowed"] is True
    assert m5["default_warranty"]["months"] == 24
    assert m5["service_score"] == max(m["service_score"] for m in small["merchants"])


# ---- 计划要求的异常样例（EDGE 对应） ----------------------------------------


def test_edge06_shipping_fee_pushes_over_budget(small):
    """O019：79000 + 2000 = 81000 分，超过 800 元（80000 分）预算。"""
    o19 = _offer(small, "O019")
    assert o19["price_cents"] == 79000
    assert _landed(o19) == 81000


def test_exact_budget_boundary(small):
    """O035：78500 + 1500 = 80000 分，恰好等于 800 元预算。"""
    o35 = _offer(small, "O035")
    assert _landed(o35) == 80000


def test_edge09_cheapest_out_of_stock_and_others_over_budget(small):
    """P006：最便宜报价缺货；有货报价到手价全部高于 80000 分。"""
    p6_offers = [o for o in small["offers"] if o["product_id"] == "P006"]
    cheapest = min(p6_offers, key=lambda o: o["price_cents"])
    assert cheapest["offer_id"] == "O018" and cheapest["stock"] == 0
    in_stock = [o for o in p6_offers if o["stock"] and o["stock"] > 0]
    assert in_stock, "必须有有货报价参与对照"
    assert all(_landed(o) > 80000 for o in in_stock)


def test_edge07_with_case_battery_cannot_prove_single_charge(small):
    """P005：厂商只提供含盒续航（32h），无 single_charge 记录。"""
    p5 = _product(small, "P005")
    bases = {s["basis"] for s in p5["battery_specs"]}
    assert bases == {"with_case"}
    assert p5["battery_specs"][0]["hours"] == 32


def test_edge03_unknown_anc(small):
    """P014：ANC 能力未知——商品级 null、模式级 null、续航 anc_state=unknown、
    anc_score 缺失；不得被当作满足 ANC 硬条件。"""
    p14 = _product(small, "P014")
    assert p14["active_noise_cancelling"] is None
    assert p14["connection_modes"][0]["active_noise_cancelling"] is None
    assert {s["anc_state"] for s in p14["battery_specs"]} == {"unknown"}
    m14 = next(m for m in small["metrics"] if m["product_id"] == "P014")
    assert m14["anc_score"] is None


def test_missing_key_params_p002(small):
    """P002：续航/重量/评分/评价数未知（null，不是 0）。"""
    p2 = _product(small, "P002")
    assert p2["battery_specs"] is None
    assert p2["weight_specs"] is None
    assert p2["battery_applicability"] == "applicable"
    m2 = next(m for m in small["metrics"] if m["product_id"] == "P002")
    assert m2["rating"] is None and m2["review_count"] is None


def test_product_all_offers_out_of_stock(small):
    """P015：旗舰商品两条报价全部缺货（availability=out_of_stock 形态）。"""
    p15_offers = [o for o in small["offers"] if o["product_id"] == "P015"]
    assert len(p15_offers) >= 2
    assert all(o["stock"] == 0 for o in p15_offers)
    assert min(o["price_cents"] for o in p15_offers) >= 179900  # 超预算样例


def test_offer_with_unknown_stock(small):
    o40 = _offer(small, "O040")
    assert o40["stock"] is None  # 未知库存，不得作为有货推荐


def test_accessory_plan_variants(small):
    """P009 三种配件形态：随附(0元)/另购(计入到手价)/未知(到手价不可算)。"""
    o26 = _offer(small, "O026")
    assert o26["accessories"][0]["included"] is True
    assert o26["accessories"][0]["price_cents"] == 0
    o27 = _offer(small, "O027")
    acc = o27["accessories"][0]
    assert acc["included"] is False and acc["price_cents"] == 9900
    assert _landed(o27) == o27["price_cents"] + 9900  # 配件计入到手价
    o28 = _offer(small, "O028")
    assert o28["accessories"] is None  # 配件方案未知


def test_opened_return_override_contrast(small):
    """O015 明确允许开封试戴退货；O001 明确禁止——SC14 的正反样本。"""
    o15 = _offer(small, "O015")
    assert o15["return_policy_override"]["opened_return_allowed"] is True
    o1 = _offer(small, "O001")
    assert o1["return_policy_override"]["opened_return_allowed"] is False


def test_sweat_statement_contrast(small):
    """P004/P013 有厂商防汗声明；P014 仅 IPX5 等级、无声明（SC11 反例）。"""
    for pid in ("P004", "P013"):
        assert _product(small, pid)["sweat_use_statement"]["allowed"] is True
    p14 = _product(small, "P014")
    assert p14["sweat_use_statement"] is None
    assert p14["water_resistance"][0]["rating"] == "IPX5"


def test_ear_tip_sizes_contrast(small):
    """P005 附 S/M/L 耳塞套（SC14 正例）；P014 无 S 号（反例）。"""
    assert _product(small, "P005")["ear_tip_sizes"] == ["S", "M", "L"]
    assert "S" not in _product(small, "P014")["ear_tip_sizes"]


def test_refurbished_offer_exists(small):
    o25 = _offer(small, "O025")
    assert o25["item_condition"] == "refurbished"


def test_delivery_commitment_variety(small):
    commitments = {o["delivery"]["commitment_type"] for o in small["offers"]}
    assert {"guaranteed", "estimated"} <= commitments
    m1_delivery = next(o for o in small["offers"]
                       if o["merchant_id"] == "M001")["delivery"]
    assert m1_delivery["commitment_type"] == "guaranteed"
    assert all(o["delivery"]["destination_region"] == "CN-SH"
               for o in small["offers"])


def test_wired_product_no_power_and_no_battery(small):
    """P007：3.5mm 即插即用（requires_power=false），无电池口径。"""
    p7 = _product(small, "P007")
    mode = p7["connection_modes"][0]
    assert mode["transport"] == "wired_3_5mm"
    assert mode["requires_power"] is False
    assert p7["battery_applicability"] == "not_applicable"
    assert p7["battery_specs"] is None


def test_multipoint_products(small):
    for pid in ("P002", "P006"):
        p = _product(small, pid)
        assert p["multipoint"] is True
        assert p["max_active_devices"] >= 2


def test_gift_return_condition_sample(small):
    o41 = _offer(small, "O041")
    assert o41["gift_return_conditions"]


# ---- 来源与口径 --------------------------------------------------------------


def test_field_sources_full_coverage(small):
    from scripts.commerce_schema import iter_original_leaves

    for entity_type, name in (("merchant", "merchants.json"),
                              ("product", "products.json"),
                              ("product_metrics", "product_metrics.json"),
                              ("offer", "offers.json")):
        for entity in _load(small["dir"], name):
            expected = {path for path, _ in iter_original_leaves(entity, entity_type)}
            assert set(entity["field_sources"]) == expected, \
                (entity.get("product_id") or entity.get("offer_id")
                 or entity.get("merchant_id"))
            for path, source in entity["field_sources"].items():
                assert source["source_type"] == "simulated"
                assert source["source_ref"].startswith("demo://small-v1/")
                if dict(iter_original_leaves(entity, entity_type))[path] is None:
                    assert source["missing_reason"] in {
                        "not_collected", "not_applicable", "not_disclosed"}


def test_delivery_leaves_share_observed_at(small):
    for o in small["offers"]:
        observed = {s["observed_at"] for path, s in o["field_sources"].items()
                    if path.startswith("delivery.")}
        assert len(observed) == 1, o["offer_id"]


def test_no_zero_pretending_unknown(small):
    """禁止用 0 冒充未知：缺失续航/重量一律 null。"""
    for p in small["products"]:
        for s in (p["battery_specs"] or []):
            assert s["hours"] is None or s["hours"] > 0
        for s in (p["weight_specs"] or []):
            assert s["grams"] is None or s["grams"] > 0


@pytest.mark.parametrize("profile,counts", [
    ("integration", {"merchants": 10, "products": 100}),
    ("demo", {"merchants": 50, "products": 500}),
])
def test_larger_profiles_generate_and_validate(profile, counts, tmp_path):
    """三档生成器可用性冒烟：数量达标且通过全量校验（验收以 small 档为准）。"""
    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile
    from scripts.validate_demo_data import validate_data_dir

    data_dir = tmp_path / profile
    manifest = generate_profile(profile, data_dir, DEFAULT_SEED)
    for name, expected in counts.items():
        assert manifest["counts"][name] == expected
    assert manifest["counts"]["offers"] >= (400 if profile == "integration" else 2500)
    assert validate_data_dir(data_dir) == []


# ---- F2/F3 整改回归：跨档身份稳定 + ANC 续航参数一致 ------------------------


def test_cross_profile_product_identity_consistent(tmp_path):
    """F2：integration 与 demo 共同商品 ID 必须指向同一 identity_key。"""
    import json

    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    integ_dir, demo_dir = tmp_path / "integ", tmp_path / "demo"
    generate_profile("integration", integ_dir, DEFAULT_SEED)
    generate_profile("demo", demo_dir, DEFAULT_SEED)

    def identities(data_dir):
        items = json.loads((data_dir / "products.json").read_text("utf-8"))["items"]
        return {p["product_id"]: (p["identity_key"], p["brand"], p["model"],
                                  p["variant"]) for p in items}

    integ, demo = identities(integ_dir), identities(demo_dir)
    common = set(integ) & set(demo)
    assert len(common) >= 95  # 15 锚点 + 85 个程序商品（P101–P185）中的绝大多数
    mismatch = [pid for pid in common if integ[pid] != demo[pid]]
    assert mismatch == [], mismatch[:5]


def test_anc_battery_pairing_consistent_in_expanded_profiles(tmp_path):
    """F3：扩大档中同 mode 同口径的 ANC 开启续航不得长于关闭续航。"""
    import json

    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    for profile in ("integration", "demo"):
        data_dir = tmp_path / profile
        generate_profile(profile, data_dir, DEFAULT_SEED)
        items = json.loads((data_dir / "products.json").read_text("utf-8"))["items"]
        for p in items:
            specs = [s for s in (p["battery_specs"] or []) if isinstance(s, dict)]
            for on in specs:
                for off in specs:
                    if (on.get("anc_state") == "on" and off.get("anc_state") == "off"
                            and on.get("basis") == off.get("basis")
                            and on.get("mode_id") == off.get("mode_id")
                            and on.get("test_conditions") == off.get("test_conditions")
                            and on.get("hours") is not None
                            and off.get("hours") is not None):
                        assert on["hours"] <= off["hours"], (
                            p["product_id"], on["hours"], off["hours"])


def test_with_case_battery_not_shorter_than_single(tmp_path):
    """含盒累计续航应不短于单次（同一基准推导，参数相互合理）。"""
    import json

    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    data_dir = tmp_path / "demo"
    generate_profile("demo", data_dir, DEFAULT_SEED)
    items = json.loads((data_dir / "products.json").read_text("utf-8"))["items"]
    for p in items:
        specs = [s for s in (p["battery_specs"] or []) if isinstance(s, dict)]
        singles = [s["hours"] for s in specs if s["basis"] == "single_charge"
                   and s.get("hours") is not None]
        cases = [s["hours"] for s in specs if s["basis"] == "with_case"
                 and s.get("hours") is not None]
        if singles and cases:
            assert max(cases) >= max(singles), (p["product_id"], singles, cases)
