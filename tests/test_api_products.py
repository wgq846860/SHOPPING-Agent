"""S1.3 API 测试：传统购物四个 GET 接口（api.v0.1 §3–§6）。

验收覆盖（计划 S1.3 + 契约 §8）：字面搜索、参数重复/非法、分页越界、
unknown 价/评分、有货最低到手价、稳定平手、报价覆盖政策、不存在资源；
另有筛选组合、特殊搜索字符、多商家关联、详情与报价事实一致。

时间相关行为（报价过期）通过 app.state.clock 注入固定参考时间，与真实
时钟无关：参考时间 2026-09-30T12:00+08:00——O001（有效至 2026-09-29）
已过期，O017（有效至 2026-10-15）未过期。

锚点数据来自生成器固定种子（small-v1）：
P001 报价 O001(69900,过期)/O003(71900)/O004(69900+1500-500)/O005(68900+3000)，
P002 无 rating/battery/weight，P015 两条报价均缺货，P009 双模式（bt/dongle）。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend.database.db import initialize_database
from backend.config import load_settings
from backend.main import create_app
from backend.services import derive

#: 固定参考时间：O001 已过期、O017 未过期
FIXED_NOW = datetime(2026, 9, 30, 12, 0, 0, tzinfo=derive.CN_TZ)


@pytest.fixture()
def api_app(small_data_dir, tmp_path):
    """隔离数据库（small_v1 批次）+ 固定时钟的应用。"""
    initialize_database(tmp_path / "db" / "shopping.sqlite3", small_data_dir)
    app = create_app(load_settings(data_dir=tmp_path))
    app.state.clock = lambda: FIXED_NOW
    return app


@pytest.fixture()
def api_client(api_app) -> TestClient:
    return TestClient(api_app)


# ---- meta 与信封 --------------------------------------------------------------


def test_list_meta_shape(api_client):
    body = api_client.get("/api/products").json()
    meta = body["meta"]
    assert set(meta) == {
        "api_version",
        "schema_version",
        "data_version",
        "request_id",
        "generated_at",
        "simulated",
    }
    assert meta["api_version"] == "api.v0.1"
    assert meta["schema_version"] == "commerce.v0.1"
    assert meta["data_version"] == "small-v1"
    assert meta["request_id"]
    assert meta["simulated"] is True
    assert set(body) == {"items", "total", "page", "page_size", "meta"}


def test_error_envelope_shape_matches_contract(api_client):
    """契约 §3 错误示例：min>max → 422，字段与原因码逐字一致。"""
    resp = api_client.get("/api/products?min_price_cents=90000&max_price_cents=80000")
    assert resp.status_code == 422
    body = resp.json()
    assert set(body) == {"error", "meta"}
    assert body["error"] == {
        "code": "VALIDATION_ERROR",
        "message": "最低价不能高于最高价",
        "details": [{"field": "min_price_cents", "reason": "range_conflict"}],
        "retryable": False,
    }
    assert body["meta"]["data_version"] == "small-v1"


# ---- 搜索：字面、特殊字符、空搜索 ---------------------------------------------


def test_browse_without_q_lists_catalog(api_client):
    """q 缺省＝目录浏览（不加文本条件），relevance＝product_id 升序。"""
    body = api_client.get("/api/products").json()
    ids = [item["product_id"] for item in body["items"]]
    assert body["total"] == 15
    assert ids == sorted(ids)


def test_empty_search_returns_empty_list(api_client):
    """计划验收：空搜索（q 显式为空/空白）返回空列表，不回退成目录浏览。"""
    for url in ("/api/products?q=", "/api/products?q=%20%20%20"):
        body = api_client.get(url).json()
        assert body["items"] == [], url
        assert body["total"] == 0, url


def test_no_match_search_returns_empty_list(api_client):
    body = api_client.get("/api/products?q=zzzz-not-a-product").json()
    assert body["items"] == []
    assert body["total"] == 0


def test_search_literal_substring_case_insensitive(api_client):
    """字面子串搜索覆盖 brand/product_name/model，不区分大小写。"""
    assert api_client.get("/api/products?q=metro").json()["total"] == 2
    assert api_client.get("/api/products?q=METRO").json()["total"] == 2
    by_model = api_client.get("/api/products?q=metro1").json()
    assert [i["product_id"] for i in by_model["items"]] == ["P001"]
    by_name = api_client.get("/api/products?q=Metro%20One").json()
    assert [i["product_id"] for i in by_name["items"]] == ["P001"]


def test_search_special_chars_are_literal(api_client):
    """% 与 _ 不当通配符：metro% / metro_ 都不得匹配 Metro 系列。"""
    for q in ("metro%", "metro_", "%", "_"):
        body = api_client.get("/api/products", params={"q": q}).json()
        assert body["items"] == [], q
        assert body["total"] == 0, q


# ---- 参数校验：重复、未知、越界 -----------------------------------------------


def test_duplicate_params_rejected(api_client):
    resp = api_client.get("/api/products?brand=A&brand=B")
    assert resp.status_code == 422
    detail = resp.json()["error"]["details"][0]
    assert detail["field"] == "brand"
    assert detail["reason"] == "duplicate_parameter"


def test_unknown_param_rejected(api_client):
    resp = api_client.get("/api/products?free_shipping=1")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason"] == "unknown_parameter"


@pytest.mark.parametrize(
    "url",
    [
        "/api/products?page=0",
        "/api/products?page=abc",
        "/api/products?page_size=0",
        "/api/products?page_size=101",
        "/api/products?min_price_cents=-1",
        "/api/products?min_price_cents=abc",
        "/api/products?min_rating=6",
        "/api/products?min_rating=-0.5",
        "/api/products?min_rating=abc",
        "/api/products?sub_category=hovercrafts",
        "/api/products?sort=price_middle",
        "/api/products?destination_region=CN-BJ",
        "/api/products?brand=%20%20",
        "/api/products?brand=" + "x" * 81,
        "/api/products?q=" + "x" * 101,
        # 非标准数字字面量（FIX-S1.3-AUDIT P2）：Python int/float 会放过这些写法
        "/api/products?page=1_0",
        "/api/products?page_size=1_0",
        "/api/products?min_price_cents=1_000",
        "/api/products?max_price_cents=0x10",
        "/api/products?min_rating=1_5",
        "/api/products?min_rating=1e2",
    ],
)
def test_invalid_params_rejected(api_client, url):
    resp = api_client.get(url)
    assert resp.status_code == 422, url
    body = resp.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"], url


def test_detail_rejects_any_query_params(api_client):
    resp = api_client.get("/api/products/P001?verbose=1")
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason"] == "unknown_parameter"


def test_id_length_limited_to_64(api_client):
    resp = api_client.get("/api/products/" + "P" * 65)
    assert resp.status_code == 422
    assert resp.json()["error"]["details"][0]["reason"] == "too_long"


# ---- 分页与排序 ---------------------------------------------------------------


def test_page_out_of_range_returns_empty_with_total(api_client):
    body = api_client.get("/api/products?page=50&page_size=24").json()
    assert body["items"] == []
    assert body["total"] == 15
    assert body["page"] == 50
    assert body["page_size"] == 24


def test_pagination_slices_stable_order(api_client):
    full = [i["product_id"] for i in api_client.get("/api/products?page_size=100").json()["items"]]
    page2 = api_client.get("/api/products?page=2&page_size=5").json()
    assert [i["product_id"] for i in page2["items"]] == full[5:10]
    assert page2["total"] == 15


def test_sort_price_asc_nulls_last(api_client):
    body = api_client.get("/api/products?sort=price_asc&page_size=100").json()
    items = body["items"]
    prices = [
        i["display_offer"]["landed_price_cents"] if i["display_offer"] else None
        for i in items
    ]
    known = [p for p in prices if p is not None]
    assert known == sorted(known)
    # 无展示价（P015 全部缺货）永远排最后
    assert items[-1]["product_id"] == "P015"
    assert items[-1]["display_offer"] is None


def test_sort_price_desc_nulls_last(api_client):
    items = api_client.get("/api/products?sort=price_desc&page_size=100").json()["items"]
    known = [
        i["display_offer"]["landed_price_cents"]
        for i in items
        if i["display_offer"] is not None
    ]
    assert known == sorted(known, reverse=True)
    assert items[-1]["product_id"] == "P015"


def test_sort_rating_desc_nulls_last(api_client):
    items = api_client.get("/api/products?sort=rating_desc&page_size=100").json()["items"]
    ratings = [i["rating"] for i in items]
    assert items[0]["product_id"] == "P015"  # 4.8 最高分（缺货不影响评分排序）
    assert items[-1]["product_id"] == "P002"  # rating 未知永远最后
    assert items[-1]["rating"] is None
    known = [r for r in ratings if r is not None]
    assert known == sorted(known, reverse=True)


def test_sort_stable_across_requests(api_client):
    first = api_client.get("/api/products?sort=price_asc&page_size=100").json()["items"]
    second = api_client.get("/api/products?sort=price_asc&page_size=100").json()["items"]
    assert [i["product_id"] for i in first] == [i["product_id"] for i in second]


# ---- 价格筛选与 unknown 价/评分 ------------------------------------------------


def test_price_filter_excludes_unknown_price(api_client):
    """有价格筛选时排除无展示价商品；无筛选时照常展示并标记缺货。"""
    filtered = api_client.get("/api/products?min_price_cents=0&page_size=100").json()
    assert "P015" not in [i["product_id"] for i in filtered["items"]]
    unfiltered = api_client.get("/api/products?page_size=100").json()
    p015 = next(i for i in unfiltered["items"] if i["product_id"] == "P015")
    assert p015["display_offer"] is None
    assert p015["availability"] == "out_of_stock"


def test_price_filter_closed_interval_on_display_price(api_client):
    """P001 展示到手价 70900（O004：69900+1500-500）；闭区间包含端点。"""
    body = api_client.get(
        "/api/products?min_price_cents=70900&max_price_cents=70900"
    ).json()
    assert [i["product_id"] for i in body["items"]] == ["P001"]


def test_min_rating_excludes_missing_rating(api_client):
    assert "P002" not in [
        i["product_id"] for i in api_client.get("/api/products?min_rating=0.1").json()["items"]
    ]
    assert "P002" in [
        i["product_id"] for i in api_client.get("/api/products").json()["items"]
    ]


def test_filter_combination(api_client):
    body = api_client.get(
        "/api/products?q=metro&max_price_cents=80000&sort=price_asc"
    ).json()
    ids = [i["product_id"] for i in body["items"]]
    # P002 展示价 69900（O002）低于 P001 的 70900（O004）
    assert ids == ["P002", "P001"]


# ---- display_offer：有货最低到手价 ---------------------------------------------


def test_display_offer_is_lowest_eligible_landed(api_client):
    """P001：O001 最便宜但已过期，展示价取合格报价中最低的 O004（70900）。"""
    item = next(
        i
        for i in api_client.get("/api/products?page_size=100").json()["items"]
        if i["product_id"] == "P001"
    )
    display = item["display_offer"]
    assert display["offer_id"] == "O004"
    assert display["merchant_id"] == "M003"
    assert display["landed_price_cents"] == 70900
    assert display["mode_id"] == "bt"
    assert item["availability"] == "in_stock"
    assert display["derivation"]["landed_price_cents"]["rule"] == "landed_price.v1"
    assert display["derivation"]["delivery_window"]["rule"] == "delivery_window.v1"


def test_delivery_window_derived_from_observed_date(api_client):
    """窗口＝观测日 + min/max_days（含当天 day0）：O001 观测 09-29、1–2 天。"""
    views = api_client.get("/api/products/P001/offers").json()["items"]
    o001 = next(v for v in views if v["offer"]["offer_id"] == "O001")
    window = o001["delivery_window"]
    assert window["observed_at"] == "2026-09-29T10:00:00+08:00"
    assert window["earliest"] == "2026-09-30"
    assert window["latest"] == "2026-10-01"
    assert window["destination_region"] == "CN-SH"


# ---- 报价资格：过期、缺货、库存未知 -------------------------------------------


def test_expired_offer_flagged_and_not_displayable(api_client):
    views = api_client.get("/api/products/P001/offers").json()["items"]
    o001 = next(v for v in views if v["offer"]["offer_id"] == "O001")
    assert o001["eligible"] is False
    assert "expired" in o001["eligibility_reasons"]
    listed = api_client.get("/api/products?q=metro").json()["items"]
    p001 = next(i for i in listed if i["product_id"] == "P001")
    assert p001["display_offer"]["offer_id"] != "O001"


def test_out_of_stock_offers_returned_with_reasons(api_client):
    body = api_client.get("/api/products/P015/offers").json()
    assert body["total"] == 2  # 缺货报价不被过滤掉
    for view in body["items"]:
        assert view["eligible"] is False
        assert "out_of_stock" in view["eligibility_reasons"]
    p006 = api_client.get("/api/products/P006/offers").json()["items"]
    o018 = next(v for v in p006 if v["offer"]["offer_id"] == "O018")
    assert o018["eligible"] is False
    assert "out_of_stock" in o018["eligibility_reasons"]


def test_stock_unknown_flagged(api_client):
    views = api_client.get("/api/products/P014/offers").json()["items"]
    o040 = next(v for v in views if v["offer"]["offer_id"] == "O040")
    assert o040["offer"]["stock"] is None
    assert o040["eligible"] is False
    assert "stock_unknown" in o040["eligibility_reasons"]
    # 价格可定价但仍不算有货
    assert o040["landed_price_cents"] == 25900


def test_offers_sort_eligible_first_then_price(api_client):
    views = api_client.get("/api/products/P006/offers").json()["items"]
    order = [v["offer"]["offer_id"] for v in views]
    assert order == ["O019", "O020", "O018"]  # 合格按到手价升序，缺货最后
    lands = [v["landed_price_cents"] for v in views[:2]]
    assert lands == sorted(lands)


def test_price_tie_broken_by_offer_id(api_client):
    """同价平手稳定：O003/O005 到手价同为 71900，按 offer_id 升序，重复请求一致。"""
    first = api_client.get("/api/products/P001/offers").json()["items"]
    second = api_client.get("/api/products/P001/offers").json()["items"]
    order = [v["offer"]["offer_id"] for v in first]
    assert order == ["O004", "O003", "O005", "O001"]
    assert order == [v["offer"]["offer_id"] for v in second]
    o003 = next(v for v in first if v["offer"]["offer_id"] == "O003")
    o005 = next(v for v in first if v["offer"]["offer_id"] == "O005")
    assert o003["landed_price_cents"] == o005["landed_price_cents"] == 71900


# ---- 配件方案与模式 ------------------------------------------------------------


def test_mode_plan_prices_required_accessory(api_client):
    """P009 dongle 模式：O027 必要配件 9900 计入到手价，O028 配件未知不合格。"""
    body = api_client.get("/api/products/P009/offers?mode_id=dongle").json()
    by_id = {v["offer"]["offer_id"]: v for v in body["items"]}
    assert by_id["O026"]["accessory_total_cents"] == 0  # 配件已包含
    assert by_id["O026"]["landed_price_cents"] == 79900
    assert by_id["O027"]["accessory_total_cents"] == 9900
    assert by_id["O027"]["landed_price_cents"] == 84800  # 74900+0+9900
    assert by_id["O028"]["landed_price_cents"] is None
    assert by_id["O028"]["eligible"] is False
    assert "plan_incomplete" in by_id["O028"]["eligibility_reasons"]
    assert [v["offer"]["offer_id"] for v in body["items"]] == ["O026", "O027", "O028"]


def test_default_mode_ignores_unneeded_accessory(api_client):
    """默认 bt 模式不为 dongle 收费（不为不必要配件计价）。"""
    views = api_client.get("/api/products/P009/offers").json()["items"]
    by_id = {v["offer"]["offer_id"]: v for v in views}
    assert all(v["mode_id"] == "bt" for v in views)
    assert by_id["O027"]["accessory_total_cents"] == 0
    assert by_id["O027"]["landed_price_cents"] == 74900
    assert by_id["O028"]["eligible"] is True


def test_mode_not_on_product_rejected(api_client):
    resp = api_client.get("/api/products/P001/offers?mode_id=quantum")
    assert resp.status_code == 422
    detail = resp.json()["error"]["details"][0]
    assert detail["field"] == "mode_id"
    assert detail["reason"] == "invalid_mode"


# ---- 报价覆盖政策 --------------------------------------------------------------


def test_offer_override_policy_wins_and_sourced(api_client):
    views = api_client.get("/api/products/P001/offers").json()["items"]
    o001 = next(v for v in views if v["offer"]["offer_id"] == "O001")
    assert o001["policy_origin"] == "offer_override"
    assert o001["effective_return_policy"]["days"] == 7
    assert o001["effective_return_policy"]["exceptions"] == ["不接受开封试戴退货"]
    source = o001["field_sources"]["effective_return_policy.days"]
    assert source["source_ref"] == "demo://small-v1/O001"
    assert o001["derivation"]["effective_return_policy"]["rule"] == "return_policy.v1"


def test_merchant_default_policy_when_no_override(api_client):
    views = api_client.get("/api/products/P001/offers").json()["items"]
    o004 = next(v for v in views if v["offer"]["offer_id"] == "O004")
    assert o004["policy_origin"] == "merchant_default"
    merchant = api_client.get("/api/merchants/M003").json()["merchant"]
    assert o004["effective_return_policy"] == merchant["default_return_policy"]
    source = o004["field_sources"]["effective_return_policy.days"]
    assert source["source_ref"] == "demo://small-v1/M003"


# ---- 多商家关联与事实一致性 -----------------------------------------------------


def test_multi_merchant_offers_consistent(api_client):
    views = api_client.get("/api/products/P001/offers").json()["items"]
    merchant_ids = {v["merchant"]["merchant_id"] for v in views}
    assert merchant_ids == {"M001", "M002", "M003", "M004"}
    for view in views:
        fetched = api_client.get(f"/api/merchants/{view['merchant']['merchant_id']}").json()
        assert view["merchant"] == fetched["merchant"]
        assert view["offer"]["merchant_id"] == view["merchant"]["merchant_id"]
        assert view["offer"]["product_id"] == "P001"


def test_detail_and_offers_facts_consistent(api_client):
    detail = api_client.get("/api/products/P001").json()
    assert detail["product"]["product_id"] == "P001"
    assert detail["metrics"]["product_id"] == "P001"
    listed = api_client.get("/api/products?q=metro").json()["items"]
    summary = next(i for i in listed if i["product_id"] == "P001")
    for key in ("brand", "product_name", "sub_category", "form_factor", "wireless",
                "active_noise_cancelling", "battery_specs", "weight_specs", "image_path"):
        assert summary[key] == detail["product"][key], key
    assert summary["rating"] == detail["metrics"]["rating"]
    assert summary["review_count"] == detail["metrics"]["review_count"]

    for view in api_client.get("/api/products/P001/offers").json()["items"]:
        assert view["offer"]["data_version"] == detail["product"]["data_version"]
        # 到手价恒等式可重算
        if view["landed_price_cents"] is not None:
            o = view["offer"]
            assert view["landed_price_cents"] == (
                o["price_cents"]
                + o["shipping_fee_cents"]
                + view["accessory_total_cents"]
                - o["instant_discount_cents"]
            )


def test_landed_price_identity_all_offers(api_client):
    """全部报价的到手价都能按 landed_price.v1 恒等式重算。"""
    for pid in [f"P{i:03d}" for i in range(1, 16)]:
        for view in api_client.get(f"/api/products/{pid}/offers").json()["items"]:
            o = view["offer"]
            if view["landed_price_cents"] is None:
                continue
            assert view["landed_price_cents"] == (
                o["price_cents"]
                + o["shipping_fee_cents"]
                + view["accessory_total_cents"]
                - o["instant_discount_cents"]
            ), o["offer_id"]


# ---- 来源与派生结构 -------------------------------------------------------------


def test_summary_field_sources_cover_original_leaves(api_client):
    items = {i["product_id"]: i for i in api_client.get("/api/products?q=metro").json()["items"]}
    p001 = items["P001"]
    sources = p001["field_sources"]
    for key in ("brand", "product_name", "sub_category", "form_factor", "wireless",
                "active_noise_cancelling", "image_path", "rating", "review_count"):
        assert key in sources, key
    assert "battery_specs[0].hours" in sources
    assert "weight_specs[0].grams" in sources
    # 派生字段不冒充原始来源
    assert "availability" not in sources
    assert "display_offer" not in sources
    derivation = p001["derivation"]
    assert derivation["availability"]["rule"] == "availability.v1"
    assert derivation["display_offer_id"]["rule"] == "display_offer.v1"

    p002 = items["P002"]
    assert p002["battery_specs"] is None
    assert p002["field_sources"]["battery_specs"]["missing_reason"] == "not_collected"


def test_offerview_structure(api_client):
    view = api_client.get("/api/products/P001/offers").json()["items"][0]
    assert set(view) == {
        "offer",
        "merchant",
        "mode_id",
        "accessory_total_cents",
        "landed_price_cents",
        "delivery_window",
        "effective_return_policy",
        "effective_warranty",
        "policy_origin",
        "warranty_origin",
        "eligible",
        "eligibility_reasons",
        "field_sources",
        "derivation",
    }
    assert set(view["derivation"]) == {
        "landed_price_cents",
        "accessory_total_cents",
        "delivery_window",
        "effective_return_policy",
        "effective_warranty",
    }
    # 原始实体内嵌且自带来源
    assert view["offer"]["field_sources"]
    assert view["merchant"]["field_sources"]


# ---- 不存在资源 -----------------------------------------------------------------


def test_product_not_found_404(api_client):
    resp = api_client.get("/api/products/P9999")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"] == {
        "code": "PRODUCT_NOT_FOUND",
        "message": "商品不存在",
        "details": [{"field": "id", "reason": "not_found"}],
        "retryable": False,
    }
    assert body["meta"]["data_version"] == "small-v1"


def test_product_offers_not_found_404(api_client):
    resp = api_client.get("/api/products/P9999/offers")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PRODUCT_NOT_FOUND"
    assert resp.json()["error"]["message"] == "商品不存在，无法读取报价"


def test_merchant_not_found_404(api_client):
    resp = api_client.get("/api/merchants/M9999")
    assert resp.status_code == 404
    assert resp.json()["error"] == {
        "code": "MERCHANT_NOT_FOUND",
        "message": "商家不存在",
        "details": [{"field": "id", "reason": "not_found"}],
        "retryable": False,
    }


# ---- REVIEW-S1-20260930 F2 回归：显式模式同样受 playback_supported 约束 ---------


def _batch_with_p001_playback(small_data_dir, tmp_path, value):
    """复制 small-v1 批次并把 P001 全部模式的 playback_supported 改为 value。

    value=None（未知听音）时同步把对应来源的 missing_reason 改为 not_collected，
    保证批次本身仍通过目录校验（false/null 是合法数据，不是校验错误）。
    """
    import json as _json

    data_dir = tmp_path / "batch-playback"
    data_dir.mkdir()
    for fname in ("manifest.json", "merchants.json", "products.json",
                  "offers.json", "product_metrics.json"):
        (data_dir / fname).write_bytes((small_data_dir / fname).read_bytes())
    products = _json.loads((data_dir / "products.json").read_text("utf-8"))
    target = next(p for p in products["items"] if p["product_id"] == "P001")
    for i, mode in enumerate(target["connection_modes"]):
        mode["playback_supported"] = value
        if value is None:
            target["field_sources"][
                f"connection_modes[{i}].playback_supported"]["missing_reason"] = (
                "not_collected")
    (data_dir / "products.json").write_text(
        _json.dumps(products, ensure_ascii=False), "utf-8")
    return data_dir


def _client_with_p001_playback(small_data_dir, tmp_path, value):
    from backend.config import load_settings

    data_dir = _batch_with_p001_playback(small_data_dir, tmp_path, value)
    initialize_database(tmp_path / "db" / "shopping.sqlite3", data_dir)
    app = create_app(load_settings(data_dir=tmp_path))
    app.state.clock = lambda: FIXED_NOW
    return TestClient(app)


@pytest.mark.parametrize("value", [False, None])
def test_explicit_unplayable_mode_not_eligible(small_data_dir, tmp_path, value):
    """F2：显式指定不支持/未知听音的模式不得 eligible=true（与默认路径同规则）。"""
    client = _client_with_p001_playback(small_data_dir, tmp_path, value)
    resp = client.get("/api/products/P001/offers", params={"mode_id": "bt"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items
    for view in items:
        assert view["eligible"] is False, (view["offer"]["offer_id"],
                                           view["eligibility_reasons"])
        assert "mode_unavailable" in view["eligibility_reasons"]


def test_explicit_playable_mode_stays_eligible(small_data_dir, tmp_path):
    """F2 对照组：playback_supported=true 且其他条件满足时仍合格（O001 仅因过期不合格）。"""
    client = _client_with_p001_playback(small_data_dir, tmp_path, True)
    by_id = {v["offer"]["offer_id"]: v
             for v in client.get("/api/products/P001/offers",
                                 params={"mode_id": "bt"}).json()["items"]}
    assert by_id["O004"]["eligible"] is True
    assert by_id["O001"]["eligible"] is False
    assert "mode_unavailable" not in by_id["O001"]["eligibility_reasons"]


def test_default_mode_unplayable_means_no_display_offer(small_data_dir, tmp_path):
    """F2 回归：默认路径不受影响——全部模式不可听音时无合格报价、无展示价。"""
    client = _client_with_p001_playback(small_data_dir, tmp_path, False)
    body = client.get("/api/products/P001/offers").json()
    assert body["items"]
    assert all(not v["eligible"] for v in body["items"])
    listed = client.get("/api/products", params={"q": "metro1"}).json()["items"]
    p001 = next(i for i in listed if i["product_id"] == "P001")
    assert p001["display_offer"] is None


def test_playback_false_is_valid_data(small_data_dir, tmp_path):
    """F2 前提：playback_supported=false 是合法数据，校验器不报错（探针前提）。"""
    from scripts.validate_demo_data import validate_data_dir

    data_dir = _batch_with_p001_playback(small_data_dir, tmp_path, False)
    assert validate_data_dir(data_dir) == []


# ---- REVIEW-S1-20260930 F3 回归：超长十进制参数受控 422 ------------------------


@pytest.mark.parametrize("param", ["page", "min_price_cents", "max_price_cents", "page_size"])
def test_huge_integer_params_return_422(api_client, param):
    """F3：4301 位十进制串不得触发未受控 500，必须 422 VALIDATION_ERROR 信封。"""
    resp = api_client.get("/api/products", params={param: "9" * 4301})
    assert resp.status_code == 422, (param, resp.status_code, resp.text[:120])
    body = resp.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["error"]["details"][0]["field"] == param
    assert body["meta"]["data_version"] == "small-v1"


def test_long_but_reasonable_integers_still_work(api_client):
    """F3 对照组：位数上限内的合法数值不受影响（远页空列表、高价全过滤）。"""
    body = api_client.get("/api/products", params={"page": 10 ** 17}).json()
    assert body["items"] == []
    assert body["page"] == 10 ** 17
    assert body["total"] == 15
    body = api_client.get("/api/products", params={"min_price_cents": 10 ** 15}).json()
    assert body["total"] == 0


def test_huge_min_rating_returns_422(api_client):
    """F3：超长小数的 min_rating 同样受长度策略约束，返回 422 而非静默接受。"""
    resp = api_client.get("/api/products", params={"min_rating": "0." + "9" * 5000})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "VALIDATION_ERROR"
    assert body["details"][0]["reason"] == "too_long"


# ---- REVIEW-S1-20260930 F4 回归：不可用数据库返回受控信封 ----------------------


def _client_with_db_state(tmp_path, state):
    """构造指定数据库状态（missing/empty/no_tables/corrupt）的隔离应用客户端。"""
    import sqlite3 as _sqlite3

    from backend.config import load_settings

    data_dir = tmp_path / f"data-{state}"
    db_path = data_dir / "db" / "shopping.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if state == "empty":
        db_path.write_bytes(b"")
    elif state == "corrupt":
        db_path.write_bytes(b"not a sqlite database")
    elif state == "no_tables":
        conn = _sqlite3.connect(str(db_path))
        try:
            conn.execute("CREATE TABLE unrelated (x INTEGER)")
            conn.commit()
        finally:
            conn.close()
    # state == "missing"：不创建任何文件
    app = create_app(load_settings(data_dir=data_dir))
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize("state", ["missing", "empty", "no_tables", "corrupt"])
def test_unavailable_database_returns_503_envelope(tmp_path, state):
    """F4：不存在/空文件/无业务表/损坏文件 → 503 DATA_UNAVAILABLE JSON 信封。"""
    client = _client_with_db_state(tmp_path, state)
    for endpoint in ("/api/products", "/api/products/P001",
                     "/api/products/P001/offers", "/api/merchants/M001"):
        resp = client.get(endpoint)
        assert resp.status_code == 503, (state, endpoint, resp.text[:120])
        assert resp.headers["content-type"].startswith("application/json"), (state, endpoint)
        body = resp.json()
        assert body["error"]["code"] == "DATA_UNAVAILABLE", (state, endpoint)
        assert body["error"]["details"][0]["reason"] == "data_unavailable"
        assert set(body) == {"error", "meta"}
        assert set(body["meta"]) == {
            "api_version", "schema_version", "data_version",
            "request_id", "generated_at", "simulated",
        }
        assert body["meta"]["data_version"] is None


def test_unexpected_exception_returns_sanitized_500(api_app, monkeypatch):
    """F4：未预期异常 → 500 INTERNAL_ERROR JSON 信封，不暴露 SQL/路径/堆栈。"""
    from backend.services import product_service

    def boom(*args, **kwargs):
        raise RuntimeError("SECRET: SELECT * FROM internal_debug_table")

    monkeypatch.setattr(product_service, "list_products", boom)
    client = TestClient(api_app, raise_server_exceptions=False)
    resp = client.get("/api/products")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert set(body) == {"error", "meta"}
    for banned in ("SECRET", "SELECT", "internal_debug", "Traceback"):
        assert banned not in resp.text, banned


# ---- N1/N2 回归（s1_overall_review_20260930）：异常日期数据映射为明确数据错误 ----


def test_huge_delivery_days_in_existing_db_maps_to_503(api_app, api_client):
    """N2 查询端：既有库中的超大配送天数 → 503 DATA_UNAVAILABLE，而非未知 500。

    入口校验（N2）封死新增路径后，本用例直接改库内 payload 模拟
    "修复前已入库的异常数据"，验证报告要求的明确数据错误映射。
    """
    import json as _json
    import sqlite3 as _sqlite3

    db_path = api_app.state.settings.db_path
    conn = _sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT payload FROM offers WHERE offer_id = 'O004'").fetchone()
        offer = _json.loads(row[0])
        offer["delivery"]["min_days"] = 1000000000
        offer["delivery"]["max_days"] = 1000000000
        conn.execute("UPDATE offers SET payload = ? WHERE offer_id = 'O004'",
                     (_json.dumps(offer, ensure_ascii=False),))
        conn.commit()
    finally:
        conn.close()

    for endpoint in ("/api/products", "/api/products/P001/offers"):
        resp = api_client.get(endpoint)
        assert resp.status_code == 503, (endpoint, resp.status_code, resp.text[:120])
        body = resp.json()
        assert body["error"]["code"] == "DATA_UNAVAILABLE", endpoint
        assert "O004" in body["error"]["message"], body["error"]["message"]
        assert "配送天数" in body["error"]["message"]
    # 纯详情不派生配送窗 → 仍 200（与复审观察一致）
    assert api_client.get("/api/products/P001").status_code == 200
