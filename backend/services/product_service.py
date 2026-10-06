"""商品服务（S1.3）：列表检索、详情、报价视图组装。

业务边界：过滤/排序/派生全部是后端确定性逻辑；这里只是把
queries.py 的检索结果经 derive.py 的规则组装成 api.v0.1 输出结构。
展示价、价格筛选与价格排序复用同一 display_offer 规则（字典 §8）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from backend.database import queries
from backend.schemas.common import Derivation, Source
from backend.schemas.offer import OfferView
from backend.schemas.product import DisplayOffer, ProductSummary
from backend.services import derive
from backend.services.errors import DataError, InvalidMode, ProductNotFound

#: ProductSummary.battery_specs / weight_specs 里的结构 ID 不需要来源
_SPEC_ID_LEAVES = {"battery_spec_id", "weight_spec_id", "mode_id"}


def list_products(
    conn: sqlite3.Connection,
    *,
    q: str | None,
    q_active: bool,
    brand: str | None,
    sub_category: str | None,
    min_price_cents: int | None,
    max_price_cents: int | None,
    min_rating: float | None,
    sort: str,
    page: int,
    page_size: int,
    region: str,
    now: datetime,
) -> dict:
    rows = queries.search_products(
        conn,
        q=q,
        q_active=q_active,
        brand=brand,
        sub_category=sub_category,
        min_rating=min_rating,
    )

    offers_by_product: dict[str, list[dict]] = {}
    merchant_ids: set[str] = set()
    for offer in queries.fetch_offers_for_products(conn, [r["product_id"] for r in rows]):
        offers_by_product.setdefault(offer["product_id"], []).append(offer)
        merchant_ids.add(offer["merchant_id"])
    merchants = queries.fetch_merchants(conn, sorted(merchant_ids))

    summaries: list[ProductSummary] = []
    for row in rows:
        metrics = row["metrics"]
        if metrics is None:
            raise DataError(f"商品 {row['product_id']} 缺少必需的指标记录")
        summary = _build_summary(
            row["product"],
            metrics,
            offers_by_product.get(row["product_id"], []),
            merchants,
            region=region,
            now=now,
        )
        # 价格筛选/排序/展示复用同一 display_offer 口径（契约 §3）
        price = summary.display_offer.landed_price_cents if summary.display_offer else None
        if min_price_cents is not None or max_price_cents is not None:
            if price is None:
                continue
            if min_price_cents is not None and price < min_price_cents:
                continue
            if max_price_cents is not None and price > max_price_cents:
                continue
        summaries.append(summary)

    summaries.sort(key=_sort_key(sort))
    total = len(summaries)
    start = (page - 1) * page_size
    items = summaries[start : start + page_size]
    return {
        "items": [s.model_dump() for s in items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _sort_key(sort: str):
    """主值排序 + product_id 平手稳定；null 永远最后（契约 §3）。"""
    if sort == "price_asc":
        return lambda s: (
            s.display_offer is None,
            s.display_offer.landed_price_cents if s.display_offer else 0,
            s.product_id,
        )
    if sort == "price_desc":
        return lambda s: (
            s.display_offer is None,
            -(s.display_offer.landed_price_cents) if s.display_offer else 0,
            s.product_id,
        )
    if sort == "rating_desc":
        return lambda s: (s.rating is None, -(s.rating or 0.0), s.product_id)
    # relevance = 默认目录顺序（product_id 升序），不伪装语义相关排名
    return lambda s: s.product_id


def get_product_detail(conn: sqlite3.Connection, product_id: str) -> dict | None:
    """完整 Product + ProductMetrics payload；缺整条指标记录视为数据错误。"""
    product = queries.fetch_product(conn, product_id)
    if product is None:
        return None
    metrics = queries.fetch_metrics(conn, product_id)
    if metrics is None:
        raise DataError(f"商品 {product_id} 缺少必需的指标记录")
    return {"product": product, "metrics": metrics}


def get_product_summary(
    conn: sqlite3.Connection,
    product_id: str,
    *,
    region: str,
    now: datetime,
) -> ProductSummary | None:
    """单商品 ProductSummary（与列表接口同一组装规则）；商品不存在为 None。

    供 S3.3 推荐响应复用：display_offer/availability 基于该商品全部
    报价计算，保证与 GET /api/products 看到的展示口径一致。
    """
    product = queries.fetch_product(conn, product_id)
    if product is None:
        return None
    metrics = queries.fetch_metrics(conn, product_id)
    if metrics is None:
        raise DataError(f"商品 {product_id} 缺少必需的指标记录")
    offers = queries.fetch_offers_for_products(conn, [product_id])
    merchants = queries.fetch_merchants(conn, sorted({o["merchant_id"] for o in offers}))
    return _build_summary(product, metrics, offers, merchants, region=region, now=now)


def get_product_offers(
    conn: sqlite3.Connection,
    product_id: str,
    *,
    region: str,
    mode_id: str | None,
    now: datetime,
) -> dict | None:
    """OfferView 列表：全部报价（含缺货/未知/过期），不因库存过滤而 404。"""
    product = queries.fetch_product(conn, product_id)
    if product is None:
        return None
    if mode_id is not None:
        modes = {m.get("mode_id") for m in product.get("connection_modes") or []}
        if mode_id not in modes:
            raise InvalidMode(mode_id)

    offers = queries.fetch_offers_for_products(conn, [product_id])
    merchants = queries.fetch_merchants(conn, sorted({o["merchant_id"] for o in offers}))

    views: list[OfferView] = []
    for offer in offers:
        merchant = merchants.get(offer["merchant_id"])
        if merchant is None:
            raise DataError(f"报价 {offer['offer_id']} 引用的商家不存在")
        plan = derive.build_plan(
            product, offer, merchant, mode_id=mode_id, region=region, now=now
        )
        views.append(_build_offer_view(plan))

    # eligible=true 先、landed_price 升序、offer_id 升序，null 价最后（契约 §5）
    views.sort(
        key=lambda v: (
            not v.eligible,
            v.landed_price_cents is None,
            v.landed_price_cents if v.landed_price_cents is not None else 0,
            v.offer["offer_id"],
        )
    )
    return {
        "product_id": product_id,
        "destination_region": region,
        "items": [v.model_dump() for v in views],
        "total": len(views),
    }


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _build_summary(
    product: dict,
    metrics: dict,
    offers: list[dict],
    merchants: dict[str, dict],
    *,
    region: str,
    now: datetime,
) -> ProductSummary:
    plans = []
    for offer in offers:
        merchant = merchants.get(offer["merchant_id"])
        if merchant is None:
            raise DataError(f"报价 {offer['offer_id']} 引用的商家不存在")
        plans.append(
            derive.build_plan(product, offer, merchant, mode_id=None, region=region, now=now)
        )
    display_plan = derive.choose_display(plans)
    availability = derive.availability(plans, display_plan)

    return ProductSummary(
        product_id=product["product_id"],
        brand=product["brand"],
        product_name=product["product_name"],
        sub_category=product["sub_category"],
        form_factor=product.get("form_factor"),
        rating=metrics.get("rating"),
        review_count=metrics.get("review_count"),
        wireless=product.get("wireless"),
        active_noise_cancelling=product.get("active_noise_cancelling"),
        battery_specs=product.get("battery_specs"),
        weight_specs=product.get("weight_specs"),
        image_path=product.get("image_path"),
        availability=availability,
        display_offer=_build_display_offer(display_plan) if display_plan else None,
        field_sources=_summary_field_sources(product, metrics),
        derivation=_summary_derivation(offers),
    )


def _build_display_offer(plan: derive.OfferPlan) -> DisplayOffer:
    offer, merchant = plan.offer, plan.merchant
    offer_fs = offer.get("field_sources") or {}
    merchant_fs = merchant.get("field_sources") or {}

    sources: dict[str, Source] = {}
    if merchant_fs.get("merchant_name"):
        sources["merchant_name"] = Source(**merchant_fs["merchant_name"])
    for target, key in (
        ("delivery_window.destination_region", "delivery.destination_region"),
        ("delivery_window.commitment_type", "delivery.commitment_type"),
        ("delivery_window.observed_at", "delivery.max_days"),
    ):
        if offer_fs.get(key):
            sources[target] = Source(**offer_fs[key])

    return DisplayOffer(
        offer_id=offer["offer_id"],
        merchant_id=offer["merchant_id"],
        merchant_name=merchant["merchant_name"],
        mode_id=plan.mode_id,
        currency=offer["currency"],
        landed_price_cents=plan.landed,
        delivery_window=plan.delivery_window,
        derivation={
            # 契约 §3 示例：DisplayOffer 的到手价派生含模式必要配件输入
            "landed_price_cents": Derivation(
                rule="landed_price.v1",
                input_refs=[
                    f"{offer['offer_id']}.price_cents",
                    f"{offer['offer_id']}.shipping_fee_cents",
                    f"{offer['offer_id']}.instant_discount_cents",
                    f"{offer['offer_id']}.accessories",
                    f"{offer['product_id']}.connection_modes",
                ],
            ),
            "delivery_window": plan.delivery_derivation,
        },
        field_sources=sources,
    )


def _build_offer_view(plan: derive.OfferPlan) -> OfferView:
    offer, merchant = plan.offer, plan.merchant
    offer_fs = offer.get("field_sources") or {}

    sources: dict[str, Source] = dict(plan.policies.field_sources)
    for target, key in (
        ("delivery_window.destination_region", "delivery.destination_region"),
        ("delivery_window.commitment_type", "delivery.commitment_type"),
        ("delivery_window.observed_at", "delivery.max_days"),
    ):
        if offer_fs.get(key):
            sources[target] = Source(**offer_fs[key])

    derivations = {
        # 契约 §5 示例：OfferView 的到手价派生不含 connection_modes（在 accessory_total 里）
        "landed_price_cents": Derivation(
            rule="landed_price.v1",
            input_refs=[
                f"{offer['offer_id']}.price_cents",
                f"{offer['offer_id']}.shipping_fee_cents",
                f"{offer['offer_id']}.instant_discount_cents",
                f"{offer['offer_id']}.accessories",
            ],
        ),
        "accessory_total_cents": Derivation(
            rule="accessory_total.v1",
            input_refs=[
                f"{offer['offer_id']}.accessories",
                f"{offer['product_id']}.connection_modes",
            ],
        ),
        "delivery_window": plan.delivery_derivation,
        **plan.policies.derivations,
    }

    return OfferView(
        offer=offer,
        merchant=merchant,
        mode_id=plan.mode_id,
        accessory_total_cents=plan.accessory_total,
        landed_price_cents=plan.landed,
        delivery_window=plan.delivery_window,
        effective_return_policy=plan.policies.return_policy,
        effective_warranty=plan.policies.warranty,
        policy_origin=plan.policies.policy_origin,
        warranty_origin=plan.policies.warranty_origin,
        eligible=plan.eligible,
        eligibility_reasons=plan.reasons,
        field_sources=sources,
        derivation=derivations,
    )


def _summary_field_sources(product: dict, metrics: dict) -> dict[str, Source]:
    """ProductSummary 返回的原始叶子逐字段来源（ID/系统字段除外）。"""
    pfs = product.get("field_sources") or {}
    mfs = metrics.get("field_sources") or {}
    out: dict[str, Source] = {}

    for key in (
        "brand",
        "product_name",
        "sub_category",
        "form_factor",
        "wireless",
        "active_noise_cancelling",
        "image_path",
    ):
        if pfs.get(key):
            out[key] = Source(**pfs[key])
    out.update(_spec_sources(pfs, "battery_specs", product.get("battery_specs")))
    out.update(_spec_sources(pfs, "weight_specs", product.get("weight_specs")))
    for key in ("rating", "review_count"):
        if mfs.get(key):
            out[key] = Source(**mfs[key])
    return out


def _spec_sources(pfs: dict, name: str, specs: list | None) -> dict[str, Source]:
    """数组按 battery_specs[0].hours 展开；null 数组用单键来源。"""
    out: dict[str, Source] = {}
    if specs is None:
        if pfs.get(name):
            out[name] = Source(**pfs[name])
        return out
    for index, spec in enumerate(specs):
        for leaf in spec:
            if leaf in _SPEC_ID_LEAVES:
                continue
            source = pfs.get(f"{name}[{index}].{leaf}")
            if source:
                out[f"{name}[{index}].{leaf}"] = Source(**source)
    return out


def _summary_derivation(offers: list[dict]) -> dict[str, Derivation]:
    """availability.v1 与 display_offer.v1 的派生记录（输入引用可回查数据库）。"""
    availability_refs: list[str] = []
    selection_refs: list[str] = []
    for offer in sorted(offers, key=lambda o: o["offer_id"]):
        oid = offer["offer_id"]
        availability_refs.extend(
            [f"{oid}.stock", f"{oid}.price_cents", f"{oid}.shipping_fee_cents"]
        )
        selection_refs.extend(
            [
                f"{oid}.price_cents",
                f"{oid}.shipping_fee_cents",
                f"{oid}.instant_discount_cents",
                f"{oid}.accessories",
                f"{oid}.stock",
                f"{oid}.valid_until",
                f"{oid}.delivery.destination_region",
            ]
        )

    return {
        "availability": Derivation(rule="availability.v1", input_refs=availability_refs),
        "display_offer_id": Derivation(rule="display_offer.v1", input_refs=selection_refs),
    }
