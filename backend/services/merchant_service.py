"""商家服务（S1.3）：商家详情（完整字典 Merchant payload）。"""

from __future__ import annotations

import sqlite3

from backend.database import queries


def get_merchant(conn: sqlite3.Connection, merchant_id: str) -> dict | None:
    return queries.fetch_merchant(conn, merchant_id)
