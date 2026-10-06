"""S1.2 数据库初始化脚本：校验 data/ 并导入 SQLite。

用法：
    .venv/Scripts/python.exe scripts/init_database.py                  # 默认 data/ → data/db/shopping.sqlite3
    .venv/Scripts/python.exe scripts/init_database.py --rebuild        # 换批次时清库重建
    .venv/Scripts/python.exe scripts/init_database.py --db PATH        # 指定库文件（须在 data/db/ 下）

安全边界（计划 S1.2 要求）：重建仅作用于已确认的演示/测试数据库——
本脚本把 --db 限制在项目 data/db/ 目录内；测试请直接调用
backend.database.db 的函数配合临时目录路径。

重复导入策略：同批次 = 事务内全量替换（幂等）；不同批次 = 拒绝，
需显式 --rebuild。导入前自动执行 commerce.v0.1 全量校验。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):  # 允许 `python scripts/init_database.py` 直接运行
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import load_settings
from backend.database.db import count_rows, connect, initialize_database

ALLOWED_DB_DIRNAME = "db"


def resolve_db_path(raw: str | None) -> Path:
    """把 --db 参数解析为允许的库路径（必须位于 data/db/ 内）。"""
    settings = load_settings()
    target = Path(raw) if raw else settings.db_path
    resolved = target.resolve()
    allowed_root = (settings.data_dir / ALLOWED_DB_DIRNAME).resolve()
    if allowed_root not in resolved.parents:
        raise SystemExit(
            f"拒绝初始化 {resolved}：本脚本只允许写入 {allowed_root} 下的演示/测试数据库。"
            f"其他用途请直接调用 backend.database.db 函数（如测试临时目录）。")
    return resolved


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化演示 SQLite 数据库")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data/）")
    parser.add_argument("--db", default=None,
                        help="库文件路径（默认 settings.db_path，必须位于 data/db/ 下）")
    parser.add_argument("--rebuild", action="store_true",
                        help="库内已有不同批次时，清库后重建导入")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    if not data_dir.is_dir():
        print(f"FAIL：数据目录不存在 {data_dir}（先运行 scripts/generate_demo_data.py）")
        return 1

    db_path = resolve_db_path(args.db)
    try:
        manifest = initialize_database(db_path, data_dir, rebuild=args.rebuild)
    except ValueError as exc:
        print(f"FAIL：{exc}")
        print("提示：换批次请加 --rebuild（仅作用于上面允许的演示/测试库）。")
        return 1

    conn = connect(db_path)
    try:
        counts = count_rows(conn)
    finally:
        conn.close()
    print(f"PASS：{db_path}")
    print(f"  批次：{manifest['data_version']}（profile={manifest['profile']}，"
          f"seed={manifest['seed']}）")
    print(f"  行数：{counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
