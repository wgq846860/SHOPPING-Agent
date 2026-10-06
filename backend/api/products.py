"""传统购物数据接口（S1.3）：api.v0.1 §3–§6 的四个 GET。

- GET /api/products            列表：字面搜索、筛选、分页、四种排序
- GET /api/products/{id}       详情：完整 Product + ProductMetrics
- GET /api/products/{id}/offers 报价：全部 OfferView（含缺货/未知/过期）
- GET /api/merchants/{id}      商家详情

参数处理（契约 §3）：
- 未知参数与重复参数一律 422；detail/merchants 不接收任何查询参数；
- q 缺省＝不加文本条件（目录浏览）；q 显式传入＝搜索模式，去首尾空白后
  为空串时返回空列表（计划 S1.3 验收"空搜索返回空列表"），否则对
  brand/product_name/model 做不区分大小写的字面子串搜索，%/_ 不当通配符；
- min_price_cents/max_price_cents 闭区间，按 display_offer 到手价（未知价排除）；
- 价格筛选、价格排序与展示价复用同一 display_offer 规则（字典 §8）。
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from backend.api.errors import (
    DataUnavailable,
    MerchantNotFound as MerchantNotFoundHttp,
    ProductNotFound,
    ValidationFailed,
    make_meta,
    map_sqlite_error,
    request_id,
)
from backend.config import DEFAULT_DESTINATION_REGION
from backend.database import queries
from backend.schemas.common import (
    ALLOWED_DESTINATION_REGIONS,
    SORT_VALUES,
    SUB_CATEGORIES,
)
from backend.services import derive, merchant_service, product_service
from backend.services.errors import DataError, InvalidMode

router = APIRouter()

LIST_PARAMS = {
    "q",
    "brand",
    "sub_category",
    "min_price_cents",
    "max_price_cents",
    "min_rating",
    "destination_region",
    "page",
    "page_size",
    "sort",
}
OFFERS_PARAMS = {"destination_region", "mode_id"}
MAX_ID_CHARS = 64
#: 数值参数允许的最大十进制位数/字符数（REVIEW-S1-20260930 F3 的明确输入策略）。
#: 金额（分）与分页远用不到 18 位；超长十进制串不依赖解释器的整数转换位数
#: 上限（int('9'*4301) 会抛未受控 ValueError → 500），先按长度拒绝为 422。
MAX_INT_DIGITS = 18
MAX_NUMBER_CHARS = 100


# ---------------------------------------------------------------------------
# 连接、时钟与 meta
# ---------------------------------------------------------------------------


def _open_conn(request: Request) -> sqlite3.Connection:
    """打开连接并先核对数据批次就绪（契约 §1.2：数据不可读 → 503）。

    F4（审查 2026-09-30）：探活前移到业务查询之前——空库/损坏文件在连接
    边界就被识别为 DATA_UNAVAILABLE，而不是等业务查询抛 sqlite3 异常；
    异常路径显式关闭已打开的连接。之后的查询错误由全局 sqlite3 处理器兜底。
    """
    from backend.database.db import connect

    settings = request.app.state.settings
    if not settings.db_path.is_file():
        raise DataUnavailable("数据库文件不存在，请先运行 scripts/init_database.py")
    conn = connect(settings.db_path)
    try:
        manifest = queries.fetch_manifest(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise map_sqlite_error(exc) from exc
    if manifest is None:
        conn.close()
        raise DataUnavailable("数据库未导入数据批次，请先运行 scripts/init_database.py")
    return conn


def _now(request: Request) -> datetime:
    clock = getattr(request.app.state, "clock", None)
    if callable(clock):
        return clock()
    return datetime.now(derive.CN_TZ)


def _meta_ok(request: Request, req_id: str, conn: sqlite3.Connection) -> dict:
    settings: Settings = request.app.state.settings
    manifest = queries.fetch_manifest(conn)
    if manifest is None:
        raise DataUnavailable("数据库未导入数据批次")
    return make_meta(settings, req_id, manifest["data_version"])


def _payload(request: Request, conn: sqlite3.Connection, body: dict) -> JSONResponse:
    body["meta"] = _meta_ok(request, request_id(), conn)
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# 参数校验
# ---------------------------------------------------------------------------


def _strict_params(request: Request, allowed: set[str]) -> dict[str, str]:
    """拒绝未知参数与重复参数（如两个 brand）。"""
    values: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key not in allowed:
            raise ValidationFailed(key, "unknown_parameter", f"不支持的参数：{key}")
        if key in values:
            raise ValidationFailed(key, "duplicate_parameter", f"参数重复：{key}")
        values[key] = value
    return values


def _parse_int(values: dict[str, str], key: str, *, minimum: int | None = None) -> int | None:
    if key not in values:
        return None
    raw = values[key].strip()
    # 仅接受十进制数字（可带正负号）：Python int() 会放过 1_0、全角数字等写法
    if not re.fullmatch(r"[+-]?[0-9]+", raw):
        raise ValidationFailed(key, "invalid_integer", f"{key} 必须是整数")
    if len(raw.lstrip("+-")) > MAX_INT_DIGITS:
        raise ValidationFailed(
            key, "too_long", f"{key} 数值位数超限（最多 {MAX_INT_DIGITS} 位）")
    try:
        number = int(raw)
    except ValueError:
        # 兜底：解释器整数转换限制低于本策略时仍受控 422，不留 500 口子
        raise ValidationFailed(key, "invalid_integer", f"{key} 数值超出可解析范围") from None
    if minimum is not None and number < minimum:
        raise ValidationFailed(key, "out_of_range", f"{key} 不能小于 {minimum}")
    return number


def _parse_q(values: dict[str, str]) -> tuple[str | None, bool]:
    """返回 (搜索词, 是否搜索模式)。q 缺省＝无文本条件。"""
    if "q" not in values:
        return None, False
    return values["q"].strip(), True


def _parse_brand(values: dict[str, str]) -> str | None:
    if "brand" not in values:
        return None
    brand = values["brand"].strip()
    if not brand:
        raise ValidationFailed("brand", "blank_value", "品牌不能为空串")
    if len(brand) > 80:
        raise ValidationFailed("brand", "too_long", "品牌最长 80 字符")
    return brand


def _parse_enum(values: dict[str, str], key: str, allowed: tuple[str, ...], message: str) -> str | None:
    if key not in values:
        return None
    value = values[key].strip()
    if value not in allowed:
        raise ValidationFailed(key, "invalid_enum", message)
    return value


def _parse_min_rating(values: dict[str, str]) -> float | None:
    if "min_rating" not in values:
        return None
    raw = values["min_rating"].strip()
    # 仅接受十进制数字形式（可带符号与小数点）；float() 会放过 1_5、1e2 等写法
    if not re.fullmatch(r"[+-]?[0-9]+(\.[0-9]+)?", raw):
        raise ValidationFailed("min_rating", "invalid_number", "min_rating 必须是数字")
    if len(raw) > MAX_NUMBER_CHARS:
        raise ValidationFailed(
            "min_rating", "too_long",
            f"min_rating 数值长度超限（最多 {MAX_NUMBER_CHARS} 字符）")
    try:
        rating = float(raw)
    except ValueError:
        # 兜底：正则已排除非法形态，不为 500 留口子
        raise ValidationFailed("min_rating", "invalid_number", "min_rating 必须是数字") from None
    if not 0 <= rating <= 5:
        raise ValidationFailed("min_rating", "out_of_range", "min_rating 取值 0–5")
    return rating


def _validate_id(raw_id: str) -> str:
    if not raw_id:
        raise ValidationFailed("id", "blank_value", "资源 ID 不能为空")
    if len(raw_id) > MAX_ID_CHARS:
        raise ValidationFailed("id", "too_long", f"资源 ID 最长 {MAX_ID_CHARS} 字符")
    return raw_id


# ---------------------------------------------------------------------------
# GET /api/products
# ---------------------------------------------------------------------------


@router.get("/api/products")
def list_products(request: Request) -> JSONResponse:
    values = _strict_params(request, LIST_PARAMS)
    req_id = request_id()

    q, q_active = _parse_q(values)
    if q_active and len(q) > 100:
        raise ValidationFailed("q", "too_long", "搜索词最长 100 字符")
    brand = _parse_brand(values)
    sub_category = _parse_enum(
        values,
        "sub_category",
        SUB_CATEGORIES,
        "sub_category 不在支持的品类枚举中",
    )
    min_price = _parse_int(values, "min_price_cents", minimum=0)
    max_price = _parse_int(values, "max_price_cents", minimum=0)
    if min_price is not None and max_price is not None and min_price > max_price:
        raise ValidationFailed(
            "min_price_cents", "range_conflict", "最低价不能高于最高价"
        )
    min_rating = _parse_min_rating(values)
    region = _parse_enum(
        values,
        "destination_region",
        ALLOWED_DESTINATION_REGIONS,
        "本版仅支持配送地区 CN-SH",
    ) or DEFAULT_DESTINATION_REGION
    page = _parse_int(values, "page", minimum=1) or 1
    page_size = _parse_int(values, "page_size", minimum=1)
    if page_size is not None and page_size > 100:
        raise ValidationFailed("page_size", "out_of_range", "page_size 取值 1–100")
    page_size = page_size or 24
    sort = _parse_enum(values, "sort", SORT_VALUES, "sort 取值 relevance/price_asc/price_desc/rating_desc") or "relevance"

    conn = _open_conn(request)
    try:
        if q_active and q == "":
            # 空搜索（q 显式为空）返回空列表，不回退成目录浏览
            body = {"items": [], "total": 0, "page": page, "page_size": page_size}
        else:
            body = product_service.list_products(
                conn,
                q=q,
                q_active=q_active,
                brand=brand,
                sub_category=sub_category,
                min_price_cents=min_price,
                max_price_cents=max_price,
                min_rating=min_rating,
                sort=sort,
                page=page,
                page_size=page_size,
                region=region,
                now=_now(request),
            )
        return _payload(request, conn, body)
    except DataError as exc:
        raise DataUnavailable(str(exc), field="data") from exc
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /api/products/{id}/offers（先注册，避免与 {id} 混淆）
# ---------------------------------------------------------------------------


@router.get("/api/products/{product_id}/offers")
def product_offers(request: Request, product_id: str) -> JSONResponse:
    values = _strict_params(request, OFFERS_PARAMS)
    _validate_id(product_id)
    region = _parse_enum(
        values,
        "destination_region",
        ALLOWED_DESTINATION_REGIONS,
        "本版仅支持配送地区 CN-SH",
    ) or DEFAULT_DESTINATION_REGION
    mode_id = values.get("mode_id")
    if mode_id is not None:
        mode_id = mode_id.strip()
        if not mode_id:
            raise ValidationFailed("mode_id", "blank_value", "mode_id 不能为空串")
        if len(mode_id) > MAX_ID_CHARS:
            raise ValidationFailed("mode_id", "too_long", f"mode_id 最长 {MAX_ID_CHARS} 字符")

    conn = _open_conn(request)
    try:
        try:
            result = product_service.get_product_offers(
                conn,
                product_id,
                region=region,
                mode_id=mode_id,
                now=_now(request),
            )
        except InvalidMode as exc:
            raise ValidationFailed(
                "mode_id", "invalid_mode", f"mode_id 不属于商品 {product_id}"
            ) from exc
        except DataError as exc:
            raise DataUnavailable(str(exc), field="data") from exc
        if result is None:
            raise ProductNotFound("商品不存在，无法读取报价")
        return _payload(request, conn, result)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /api/products/{id}
# ---------------------------------------------------------------------------


@router.get("/api/products/{product_id}")
def product_detail(request: Request, product_id: str) -> JSONResponse:
    _strict_params(request, set())
    _validate_id(product_id)
    conn = _open_conn(request)
    try:
        try:
            result = product_service.get_product_detail(conn, product_id)
        except DataError as exc:
            raise DataUnavailable(str(exc), field="data") from exc
        if result is None:
            raise ProductNotFound()
        return _payload(request, conn, result)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /api/merchants/{id}
# ---------------------------------------------------------------------------


@router.get("/api/merchants/{merchant_id}")
def merchant_detail(request: Request, merchant_id: str) -> JSONResponse:
    _strict_params(request, set())
    _validate_id(merchant_id)
    conn = _open_conn(request)
    try:
        merchant = merchant_service.get_merchant(conn, merchant_id)
        if merchant is None:
            raise MerchantNotFoundHttp()
        return _payload(request, conn, {"merchant": merchant})
    finally:
        conn.close()
