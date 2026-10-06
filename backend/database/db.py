"""SQLite 数据库层（S1.2；F1 整改后版本）。

职责边界：
- 建四类实体表 + 数据批次表，启用 SQLite 外键检查；
- 导入前先跑 commerce.v0.1 全量校验（validate_data_dir），
  校验不通过不碰数据库；
- 同一 data_version 重复导入按**全量替换**处理（幂等，不悄悄堆重复行）；
- 不同 data_version 直接拒绝，防止批次混装，需显式 rebuild。

F1（验收整改）：rebuild 的原子性。
- 重建前先完整校验目标语料，校验失败时原库一字节不动；
- 建表/删表/插数据全部放在**同一个事务**里（SQLite DDL 可回滚），
  任何一步失败（校验漏网的外键/CHECK 冲突等）整体回滚，
  原库的数据与 manifest 完整保留。

设计说明：实体完整内容（含嵌套结构与 field_sources）以 JSON 存 payload 列，
另抽取 S1.3 查询要用的列为可索引列。JSON 文本与抽取列同源于同一条 payload，
写入在同一个事务里完成。布尔列存 0/1/NULL（NULL=未知，不用 0 冒充）。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backend.config import SCHEMA_VERSION

#: 建表语句逐条列出（不用 executescript：它会在执行前隐式 COMMIT，
#: 无法用于"先删后建再导入"的同一事务）。
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS merchants (
        merchant_id     TEXT PRIMARY KEY,
        merchant_name   TEXT NOT NULL,
        merchant_type   TEXT,
        region          TEXT,
        merchant_rating REAL,
        service_score   REAL,
        shipping_score  REAL,
        data_version    TEXT NOT NULL,
        payload         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS products (
        product_id              TEXT PRIMARY KEY,
        identity_key            TEXT NOT NULL UNIQUE,
        brand                   TEXT NOT NULL,
        brand_norm              TEXT NOT NULL,
        model                   TEXT NOT NULL,
        model_norm              TEXT NOT NULL,
        product_name            TEXT NOT NULL,
        name_norm               TEXT NOT NULL,
        category                TEXT NOT NULL,
        sub_category            TEXT NOT NULL,
        form_factor             TEXT,
        wireless                INTEGER,
        active_noise_cancelling INTEGER,
        data_version            TEXT NOT NULL,
        payload                 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS product_metrics (
        product_id            TEXT PRIMARY KEY
                              REFERENCES products(product_id) ON DELETE CASCADE,
        rating                REAL,
        review_count          INTEGER,
        popularity_score      REAL,
        quality_score         REAL,
        comfort_score         REAL,
        anc_score             REAL,
        sound_quality_score   REAL,
        music_score           REAL,
        call_score            REAL,
        commute_score         REAL,
        gaming_score          REAL,
        portability_score     REAL,
        fit_stability_score   REAL,
        data_version          TEXT NOT NULL,
        payload               TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS offers (
        offer_id               TEXT PRIMARY KEY,
        product_id             TEXT NOT NULL REFERENCES products(product_id)
                               ON DELETE CASCADE,
        merchant_id            TEXT NOT NULL REFERENCES merchants(merchant_id)
                               ON DELETE CASCADE,
        price_cents            INTEGER NOT NULL CHECK (price_cents >= 0),
        original_price_cents   INTEGER CHECK (original_price_cents IS NULL
                                              OR original_price_cents >= price_cents),
        shipping_fee_cents     INTEGER CHECK (shipping_fee_cents IS NULL
                                              OR shipping_fee_cents >= 0),
        instant_discount_cents INTEGER NOT NULL CHECK (instant_discount_cents >= 0
                                              AND instant_discount_cents <= price_cents),
        stock                  INTEGER CHECK (stock IS NULL OR stock >= 0),
        item_condition         TEXT,
        destination_region     TEXT,
        min_days               INTEGER,
        max_days               INTEGER,
        commitment_type        TEXT,
        valid_until            TEXT,
        data_version           TEXT NOT NULL,
        payload                TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS data_manifest (
        id                INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version    TEXT NOT NULL,
        data_version      TEXT NOT NULL,
        profile           TEXT NOT NULL,
        seed              INTEGER NOT NULL,
        generator_version TEXT NOT NULL,
        generated_at      TEXT NOT NULL,
        imported_at       TEXT NOT NULL,
        counts_json       TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_products_brand ON products(brand_norm)",
    "CREATE INDEX IF NOT EXISTS idx_products_name ON products(name_norm)",
    "CREATE INDEX IF NOT EXISTS idx_products_sub_category ON products(sub_category)",
    "CREATE INDEX IF NOT EXISTS idx_offers_product ON offers(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_offers_merchant ON offers(merchant_id)",
)

#: 删除顺序子表在前，外键开启时也不会违反约束。
DROP_STATEMENTS = (
    "DROP TABLE IF EXISTS offers",
    "DROP TABLE IF EXISTS product_metrics",
    "DROP TABLE IF EXISTS products",
    "DROP TABLE IF EXISTS merchants",
    "DROP TABLE IF EXISTS data_manifest",
)


def connect(db_path: Path | str) -> sqlite3.Connection:
    """打开连接并启用外键检查（SQLite 每个连接都要单独设置）。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    conn.commit()


def drop_all(conn: sqlite3.Connection) -> None:
    """删除全部表（独立使用时自行提交；rebuild 走事务路径，不用本函数）。"""
    for statement in DROP_STATEMENTS:
        conn.execute(statement)
    conn.commit()


def get_manifest(conn: sqlite3.Connection) -> dict | None:
    """读取批次 manifest；空库/未建表时返回 None（不抛错）。"""
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'data_manifest'"
    ).fetchone()
    if table is None:
        return None
    row = conn.execute("SELECT * FROM data_manifest WHERE id = 1").fetchone()
    return dict(row) if row else None


def _bool_to_int(value):
    if value is None:
        return None
    return 1 if value else 0


def _load_batch(data_dir: Path) -> tuple[dict, dict[str, list]]:
    """读取并校验一个数据批次，返回 (manifest, payloads)。校验失败抛 ValueError。"""
    from scripts.validate_demo_data import validate_data_dir

    data_dir = Path(data_dir)
    errors = validate_data_dir(data_dir)
    if errors:
        raise ValueError(
            "数据校验未通过，拒绝导入：" + "；".join(errors[:5])
            + (f"（共 {len(errors)} 条）" if len(errors) > 5 else ""))

    manifest = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    payloads = {}
    for name, singular in (("merchants", "merchant"), ("products", "product"),
                           ("product_metrics", "product_metrics"), ("offers", "offer")):
        wrapper = json.loads((data_dir / f"{name}.json").read_text(encoding="utf-8"))
        payloads[singular] = wrapper["items"]
    return manifest, payloads


def _replace_all_rows(conn: sqlite3.Connection, payloads: dict[str, list],
                      manifest: dict) -> None:
    """清空四表并写入新批次（调用方负责事务与提交）。"""
    for table in ("offers", "product_metrics", "products", "merchants"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM data_manifest")

    for m in payloads["merchant"]:
        conn.execute(
            """INSERT INTO merchants (merchant_id, merchant_name, merchant_type,
               region, merchant_rating, service_score, shipping_score,
               data_version, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (m["merchant_id"], m["merchant_name"], m["merchant_type"],
             m["region"], m["merchant_rating"], m["service_score"],
             m["shipping_score"], m["data_version"],
             json.dumps(m, ensure_ascii=False)))

    for p in payloads["product"]:
        conn.execute(
            """INSERT INTO products (product_id, identity_key, brand, brand_norm,
               model, model_norm, product_name, name_norm, category, sub_category,
               form_factor, wireless, active_noise_cancelling, data_version, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p["product_id"], p["identity_key"], p["brand"], p["brand"].lower(),
             p["model"], p["model"].lower(), p["product_name"],
             p["product_name"].lower(), p["category"], p["sub_category"],
             p["form_factor"], _bool_to_int(p["wireless"]),
             _bool_to_int(p["active_noise_cancelling"]), p["data_version"],
             json.dumps(p, ensure_ascii=False)))

    for rec in payloads["product_metrics"]:
        conn.execute(
            """INSERT INTO product_metrics (product_id, rating, review_count,
               popularity_score, quality_score, comfort_score, anc_score,
               sound_quality_score, music_score, call_score, commute_score,
               gaming_score, portability_score, fit_stability_score,
               data_version, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (rec["product_id"], rec["rating"], rec["review_count"],
             rec["popularity_score"], rec["quality_score"], rec["comfort_score"],
             rec["anc_score"], rec["sound_quality_score"], rec["music_score"],
             rec["call_score"], rec["commute_score"], rec["gaming_score"],
             rec["portability_score"], rec["fit_stability_score"],
             rec["data_version"], json.dumps(rec, ensure_ascii=False)))

    for o in payloads["offer"]:
        conn.execute(
            """INSERT INTO offers (offer_id, product_id, merchant_id, price_cents,
               original_price_cents, shipping_fee_cents, instant_discount_cents,
               stock, item_condition, destination_region, min_days, max_days,
               commitment_type, valid_until, data_version, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (o["offer_id"], o["product_id"], o["merchant_id"], o["price_cents"],
             o["original_price_cents"], o["shipping_fee_cents"],
             o["instant_discount_cents"], o["stock"], o["item_condition"],
             o["delivery"]["destination_region"], o["delivery"]["min_days"],
             o["delivery"]["max_days"], o["delivery"]["commitment_type"],
             o["valid_until"], o["data_version"],
             json.dumps(o, ensure_ascii=False)))

    conn.execute(
        """INSERT INTO data_manifest (id, schema_version, data_version, profile,
           seed, generator_version, generated_at, imported_at, counts_json)
           VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (manifest["schema_version"], manifest["data_version"], manifest["profile"],
         manifest["seed"], manifest["generator_version"],
         manifest["generated_at"], manifest["generated_at"],
         json.dumps(manifest["counts"], ensure_ascii=False)))


def import_data(conn: sqlite3.Connection, data_dir: Path | str, *,
                batch: tuple[dict, dict[str, list]] | None = None) -> dict:
    """校验并导入一个数据批次；返回 manifest dict。

    - 导入前执行 validate_data_dir 全量校验，任何错误都中止（不写入半批数据）；
    - 库内已有**不同** data_version 时抛 ValueError（防止混批），
      需要换批必须走 rebuild_database()；
    - 相同 data_version 重复导入 = 事务内全量替换（幂等）；
    - batch：R2 允许调用方传入已由 _load_batch 校验加载的 (manifest, payloads)
      以免重复校验；不传则自行加载校验。
    """
    manifest, payloads = batch if batch is not None else _load_batch(Path(data_dir))
    data_version = manifest["data_version"]

    existing = get_manifest(conn)
    if existing and existing["data_version"] != data_version:
        raise ValueError(
            f"数据库当前批次为 {existing['data_version']!r}，与待导入批次 "
            f"{data_version!r} 不同；如需换批请使用 rebuild_database()")

    conn.execute("BEGIN")
    try:
        _replace_all_rows(conn, payloads, manifest)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return manifest


def rebuild_database(conn: sqlite3.Connection, data_dir: Path | str, *,
                     batch: tuple[dict, dict[str, list]] | None = None) -> dict:
    """清空并重建库结构后导入新批次（仅限已确认的演示/测试数据库调用）。

    F1 原子性：
    1. 先对目标语料做全量校验——校验失败直接抛错，原库一字节不动；
    2. 删表、建表、写入放在同一个事务里（SQLite DDL 可回滚），
       插入中途任何失败整体回滚，原库数据与 manifest 完整保留。
    batch：R2 允许传入已校验加载的 (manifest, payloads)，语义同 import_data。
    """
    manifest, payloads = batch if batch is not None else _load_batch(Path(data_dir))

    conn.execute("BEGIN")
    try:
        for statement in DROP_STATEMENTS:
            conn.execute(statement)
        for statement in SCHEMA_STATEMENTS:
            conn.execute(statement)
        _replace_all_rows(conn, payloads, manifest)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return manifest


def initialize_database(db_path: Path | str, data_dir: Path | str,
                        rebuild: bool = False) -> dict:
    """命令行入口：对指定库文件建表并导入。返回 manifest dict。

    重复导入策略（已文档化）：
    - 空库/同批次 → 建表 + 全量替换（幂等）；
    - 不同批次 → 默认拒绝；仅当 rebuild=True 时走原子重建。

    R2（复验 2026-09-30）：入口先经 _load_batch 完整校验输入批次——
    manifest 为 null/非对象/缺 data_version/非法 JSON 都在这里受控抛
    ValueError，不再自行 json.loads 提前解引用未经校验的 manifest。
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    batch = _load_batch(Path(data_dir))  # 校验失败在这里抛出，库文件不被触碰
    incoming_version = batch[0]["data_version"]
    conn = connect(db_path)
    try:
        existing = get_manifest(conn)
        if existing and existing["data_version"] != incoming_version:
            if not rebuild:
                raise ValueError(
                    f"{db_path} 已包含批次 {existing['data_version']!r}；"
                    f"换批需显式 rebuild=True")
            return rebuild_database(conn, data_dir, batch=batch)
        init_schema(conn)
        return import_data(conn, data_dir, batch=batch)
    finally:
        conn.close()


def count_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """各表行数（含 manifest 恒为 1 行的校验），供验收与测试使用。"""
    counts = {}
    for table in ("merchants", "products", "product_metrics", "offers",
                  "data_manifest"):
        counts[table] = conn.execute(
            f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts
