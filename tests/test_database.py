"""S1.2 验收测试：SQLite 建表、校验后导入、外键与约束、幂等与换批。

全部使用临时目录数据库，不触碰项目 data/db/。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from backend.database.db import (
    connect,
    count_rows,
    get_manifest,
    import_data,
    initialize_database,
    rebuild_database,
)


@pytest.fixture()
def db_path(tmp_path):
    return tmp_path / "test.sqlite3"


def test_import_matches_manifest_counts(db_path, small_data_dir):
    manifest = initialize_database(db_path, small_data_dir)
    assert manifest["data_version"] == "small-v1"
    conn = connect(db_path)
    try:
        assert count_rows(conn) == {
            "merchants": 5, "products": 15, "product_metrics": 15,
            "offers": 42, "data_manifest": 1}
        row = get_manifest(conn)
        assert row["profile"] == "small"
        assert json.loads(row["counts_json"])["offers"] == 42
    finally:
        conn.close()


def test_payload_roundtrip(db_path, small_data_dir):
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT payload FROM products WHERE product_id = 'P001'").fetchone()
        payload = json.loads(row["payload"])
        assert payload["product_name"] == "Metro One"
        assert payload["field_sources"]["wireless"]["source_type"] == "simulated"
        # 布尔未知以 NULL 存储，不以 0 冒充
        row2 = conn.execute(
            "SELECT wireless, active_noise_cancelling FROM products "
            "WHERE product_id = 'P014'").fetchone()
        assert row2["wireless"] == 1
        assert row2["active_noise_cancelling"] is None
    finally:
        conn.close()


def test_extracted_columns_usable(db_path, small_data_dir):
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT product_id FROM products WHERE brand_norm = 'auraloom' "
            "ORDER BY product_id").fetchall()
        assert [r["product_id"] for r in rows] == ["P001", "P002"]
        # delivery 抽取列与 payload 同源
        row = conn.execute(
            "SELECT min_days, max_days, commitment_type FROM offers "
            "WHERE offer_id = 'O001'").fetchone()
        assert (row["min_days"], row["max_days"], row["commitment_type"]) == \
            (1, 2, "guaranteed")
    finally:
        conn.close()


def test_foreign_key_rejects_orphan_offer(db_path, small_data_dir):
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO offers (offer_id, product_id, merchant_id, price_cents,"
                " instant_discount_cents, data_version, payload)"
                " VALUES ('OX', 'P999', 'M001', 100, 0, 'small-v1', '{}')")
            conn.commit()
    finally:
        conn.close()


def test_check_constraints(db_path, small_data_dir):
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):  # 负价格
            conn.execute(
                "INSERT INTO offers (offer_id, product_id, merchant_id, price_cents,"
                " instant_discount_cents, data_version, payload)"
                " VALUES ('OY', 'P001', 'M001', -5, 0, 'small-v1', '{}')")
        with pytest.raises(sqlite3.IntegrityError):  # 直降超过售价
            conn.execute(
                "INSERT INTO offers (offer_id, product_id, merchant_id, price_cents,"
                " instant_discount_cents, data_version, payload)"
                " VALUES ('OZ', 'P001', 'M001', 100, 200, 'small-v1', '{}')")
        conn.commit()
    finally:
        conn.close()


def test_reimport_same_batch_is_idempotent(db_path, small_data_dir):
    initialize_database(db_path, small_data_dir)
    initialize_database(db_path, small_data_dir)  # 同批次再次导入
    conn = connect(db_path)
    try:
        assert count_rows(conn)["offers"] == 42  # 不悄悄堆重复行
    finally:
        conn.close()


def test_import_invalid_data_aborts(db_path, tmp_path):
    from backend.database.db import init_schema
    from scripts.generate_demo_data import generate_profile

    bad_dir = tmp_path / "bad"
    generate_profile("small", bad_dir)
    text = (bad_dir / "offers.json").read_text("utf-8").replace(
        '"price_cents": 69900', '"price_cents": -69900', 1)
    (bad_dir / "offers.json").write_text(text, "utf-8")

    conn = connect(db_path)
    try:
        init_schema(conn)
        with pytest.raises(ValueError, match="校验未通过"):
            import_data(conn, bad_dir)
        assert count_rows(conn)["products"] == 0  # 没有半批数据落库
    finally:
        conn.close()


def test_version_conflict_rejected_without_rebuild(db_path, small_data_dir, tmp_path):
    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    initialize_database(db_path, small_data_dir)
    demo_dir = tmp_path / "demo"
    generate_profile("demo", demo_dir, DEFAULT_SEED)
    with pytest.raises(ValueError, match="rebuild"):
        initialize_database(db_path, demo_dir)


def test_rebuild_switches_batch(db_path, small_data_dir, tmp_path):
    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    initialize_database(db_path, small_data_dir)
    demo_dir = tmp_path / "demo"
    generate_profile("demo", demo_dir, DEFAULT_SEED)
    rebuild_database(connect(db_path), demo_dir)
    conn = connect(db_path)
    try:
        counts = count_rows(conn)
        assert counts["products"] == 500 and counts["offers"] >= 2500
        assert get_manifest(conn)["data_version"] == "demo-v1"
    finally:
        conn.close()


# ---- F1 回归：失败的重建必须完整保留原库（数据 + manifest） ------------------


def _snapshot(db_path):
    conn = connect(db_path)
    try:
        return {"counts": count_rows(conn), "manifest": get_manifest(conn)}
    finally:
        conn.close()


def test_rebuild_with_invalid_data_preserves_original(db_path, small_data_dir, tmp_path):
    """F1 路径一：换批重建遇到校验失败 → 报错且原库一字节不动。"""
    import json

    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    initialize_database(db_path, small_data_dir)
    original = _snapshot(db_path)

    demo_dir = tmp_path / "bad_demo"
    generate_profile("demo", demo_dir, DEFAULT_SEED)
    offers_file = demo_dir / "offers.json"
    wrapper = json.loads(offers_file.read_text(encoding="utf-8"))
    wrapper["items"][0]["price_cents"] = -69900
    offers_file.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2) + "\n",
                           encoding="utf-8")

    with pytest.raises(ValueError, match="校验未通过"):
        initialize_database(db_path, demo_dir, rebuild=True)
    assert _snapshot(db_path) == original


def test_rebuild_with_midinsert_failure_preserves_original(
        db_path, small_data_dir, tmp_path, monkeypatch):
    """F1 路径二：校验被绕过时插入中途失败（identity_key UNIQUE 冲突）
    → 事务回滚，原库数据与 manifest 完整保留。"""
    import json

    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    initialize_database(db_path, small_data_dir)
    original = _snapshot(db_path)

    demo_dir = tmp_path / "dup_demo"
    generate_profile("demo", demo_dir, DEFAULT_SEED)
    products_file = demo_dir / "products.json"
    wrapper = json.loads(products_file.read_text(encoding="utf-8"))
    wrapper["items"][1]["identity_key"] = wrapper["items"][0]["identity_key"]
    products_file.write_text(json.dumps(wrapper, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")

    # 模拟"校验漏检"：绕过校验器，让冲突在插入阶段才暴露
    monkeypatch.setattr("scripts.validate_demo_data.validate_data_dir",
                        lambda data_dir, **kwargs: [])
    with pytest.raises(sqlite3.IntegrityError):
        initialize_database(db_path, demo_dir, rebuild=True)
    assert _snapshot(db_path) == original


def test_reinit_from_empty_database(db_path, small_data_dir):
    initialize_database(db_path, small_data_dir)
    db_path.unlink()  # 删库后从空数据库重新初始化
    initialize_database(db_path, small_data_dir)
    conn = connect(db_path)
    try:
        assert count_rows(conn)["merchants"] == 5
    finally:
        conn.close()


def test_import_data_requires_valid_payload(db_path, small_data_dir):
    """import_data 内部也会跑校验（不只依赖 CLI 先校验）。"""
    from backend.database.db import init_schema

    conn = connect(db_path)
    try:
        init_schema(conn)
        # 目录缺失 → 校验错误 → 拒绝导入，且库内没有半批数据
        with pytest.raises(ValueError):
            import_data(conn, small_data_dir / "no_such_dir")
        assert count_rows(conn)["products"] == 0
    finally:
        conn.close()


def test_cli_rejects_db_path_outside_data_db():
    """初始化脚本只允许写 data/db/ 下的演示/测试数据库。"""
    from scripts.init_database import resolve_db_path

    outside = __import__("pathlib").Path(__file__).resolve().parent.parent / "evil.sqlite3"
    with pytest.raises(SystemExit):
        resolve_db_path(str(outside))


# ---- R2 回归（s1_f1_f4_reaudit_20260930）：初始化入口不得在校验前解引用 manifest ----


_R2_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _broken_manifest_batch(small_data_dir, tmp_path, name, inject):
    """复制合法批次并把 manifest.json 写为指定状态（raw 字符串或改写函数）。"""
    data = tmp_path / name
    data.mkdir()
    for fname in ("manifest.json", "merchants.json", "products.json",
                  "offers.json", "product_metrics.json"):
        (data / fname).write_bytes((small_data_dir / fname).read_bytes())
    if callable(inject):
        manifest = json.loads((data / "manifest.json").read_text("utf-8"))
        inject(manifest)
        (data / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), "utf-8")
    else:
        (data / "manifest.json").write_text(inject, encoding="utf-8")
    return data


_R2_STATES = [
    ("null", "null"),
    ("int", "42"),
    ("missing_data_version", lambda m: m.pop("data_version")),
    ("invalid_json", "{oops"),
]


@pytest.mark.parametrize("state,inject", _R2_STATES, ids=[s for s, _ in _R2_STATES])
def test_r2_init_rejects_broken_manifest_on_new_db(db_path, small_data_dir, tmp_path,
                                                   state, inject):
    """R2：manifest=null/非对象/缺 data_version/非法 JSON → 新库路径受控 ValueError，
    且校验先于建库——不留下空库/半初始化文件。"""
    from backend.database.db import initialize_database

    data = _broken_manifest_batch(small_data_dir, tmp_path, f"r2-new-{state}", inject)
    with pytest.raises(ValueError, match="数据校验未通过"):
        initialize_database(db_path, data)
    assert not db_path.exists()


@pytest.mark.parametrize("state,inject", _R2_STATES, ids=[s for s, _ in _R2_STATES])
def test_r2_init_rejects_broken_manifest_on_existing_db(db_path, small_data_dir, tmp_path,
                                                        state, inject):
    """R2：已有批次的库遇到畸形 manifest → 受控 ValueError，原库全内容与 manifest 不变。"""
    from backend.database.db import initialize_database

    initialize_database(db_path, small_data_dir)
    original = _snapshot(db_path)
    data = _broken_manifest_batch(small_data_dir, tmp_path, f"r2-exist-{state}", inject)
    with pytest.raises(ValueError, match="数据校验未通过"):
        initialize_database(db_path, data)
    assert _snapshot(db_path) == original


def test_r2_cli_rejects_broken_manifest_without_traceback(small_data_dir, tmp_path):
    """R2：CLI 初始化路径对畸形 manifest 退出 1 + FAIL，无 traceback，且不创建库文件。

    CLI 的 --db 只允许写项目 data/db/ 下（脚本自身守卫），这里用尚不存在的
    库名：修复后校验先于建库，文件不应被创建；finally 兜底清理以隔离红灯态。
    """
    import subprocess
    import sys

    data = _broken_manifest_batch(small_data_dir, tmp_path, "r2-cli", "null")
    target = _R2_PROJECT_ROOT / "data" / "db" / "r2-cli-broken-manifest.sqlite3"
    try:
        script = _R2_PROJECT_ROOT / "scripts" / "init_database.py"
        result = subprocess.run(
            [sys.executable, str(script),
             "--data-dir", str(data), "--db", str(target)],
            capture_output=True, text=True, cwd=_R2_PROJECT_ROOT)
        assert result.returncode == 1, (result.stdout, result.stderr)
        assert "FAIL" in result.stdout
        for banned in ("Traceback", "TypeError", "KeyError"):
            assert banned not in result.stderr, banned
        assert not target.exists(), "受控失败不得创建库文件"
    finally:
        if target.exists():
            target.unlink()
