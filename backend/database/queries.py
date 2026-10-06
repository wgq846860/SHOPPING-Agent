"""参数化查询层（S1.3）。

只做 SQL 检索与过滤下推：字面搜索、品牌、品类、评分筛选在库内完成；
到手价/展示报价等派生计算（display_offer.v1 等）在 services/derive.py 做，
展示价、价格筛选与价格排序复用同一 display_offer 规则。

全部查询使用参数占位符，不拼接用户输入；`%`/`_` 在 LIKE 中按字面处理
（ESCAPE '\\' 转义），不作为通配符。
"""

from __future__ import annotations

import sqlite3


def escape_like(needle: str) -> str:
    """把 % _ \\ 按字面处理，供 LIKE ... ESCAPE '\\' 使用。"""
    return needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fetch_manifest(conn: sqlite3.Connection) -> dict | None:
    from backend.database.db import get_manifest

    return get_manifest(conn)


def fetch_product(conn: sqlite3.Connection, product_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM products WHERE product_id = ?", (product_id,)
    ).fetchone()
    return _payload(row)


def fetch_metrics(conn: sqlite3.Connection, product_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM product_metrics WHERE product_id = ?", (product_id,)
    ).fetchone()
    return _payload(row)


def fetch_merchant(conn: sqlite3.Connection, merchant_id: str) -> dict | None:
    row = conn.execute(
        "SELECT payload FROM merchants WHERE merchant_id = ?", (merchant_id,)
    ).fetchone()
    return _payload(row)


def fetch_merchants(conn: sqlite3.Connection, merchant_ids: list[str]) -> dict[str, dict]:
    if not merchant_ids:
        return {}
    placeholders = ",".join("?" for _ in merchant_ids)
    rows = conn.execute(
        f"SELECT merchant_id, payload FROM merchants WHERE merchant_id IN ({placeholders})",
        list(merchant_ids),
    ).fetchall()
    return {row["merchant_id"]: _decode(row["payload"]) for row in rows}


def fetch_offers_for_products(
    conn: sqlite3.Connection, product_ids: list[str]
) -> list[dict]:
    """按 product_id 取报价 payload，按 offer_id 升序（结果稳定）。"""
    if not product_ids:
        return []
    placeholders = ",".join("?" for _ in product_ids)
    rows = conn.execute(
        f"""SELECT payload FROM offers
            WHERE product_id IN ({placeholders})
            ORDER BY offer_id""",
        list(product_ids),
    ).fetchall()
    return [_decode(row["payload"]) for row in rows]


def search_products(
    conn: sqlite3.Connection,
    *,
    q: str | None,
    q_active: bool,
    brand: str | None,
    sub_category: str | None,
    min_rating: float | None,
) -> list[dict]:
    """商品候选检索（字面搜索 + 品牌/品类/评分筛选）。

    - q_active=True：对 brand/product_name/model 做不区分大小写的字面子串搜索
      （列在导入时已小写化；%/_ 按字面转义）。q 为空串时语义上不匹配任何商品
      （"空搜索返回空列表"），由调用方直接返回空集。
    - brand：不区分大小写精确匹配；未知品牌返回空集。
    - min_rating：缺失 rating 不通过该筛选。

    返回 {"product_id", "product", "metrics"}（payload 解码后），按 product_id 升序。
    """
    conditions: list[str] = []
    params: list = []

    if q_active and q:
        pattern = f"%{escape_like(q.lower())}%"
        conditions.append(
            "(p.brand_norm LIKE ? ESCAPE '\\'"
            " OR p.name_norm LIKE ? ESCAPE '\\'"
            " OR p.model_norm LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern, pattern, pattern])
    if brand is not None:
        conditions.append("p.brand_norm = ?")
        params.append(brand.lower())
    if sub_category is not None:
        conditions.append("p.sub_category = ?")
        params.append(sub_category)
    if min_rating is not None:
        conditions.append("m.rating IS NOT NULL AND m.rating >= ?")
        params.append(min_rating)

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"""SELECT p.product_id, p.payload AS product_payload,
                   m.payload AS metrics_payload
            FROM products p
            LEFT JOIN product_metrics m ON m.product_id = p.product_id
            {where}
            ORDER BY p.product_id""",
        params,
    ).fetchall()
    return [
        {
            "product_id": row["product_id"],
            "product": _decode(row["product_payload"]),
            "metrics": _decode(row["metrics_payload"]) if row["metrics_payload"] else None,
        }
        for row in rows
    ]


def _payload(row: sqlite3.Row | None) -> dict | None:
    return _decode(row["payload"]) if row else None


def _decode(raw: str) -> dict:
    import json

    return json.loads(raw)
