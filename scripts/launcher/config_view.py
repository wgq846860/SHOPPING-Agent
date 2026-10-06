"""配置概览与 API 配置向导的纯逻辑。

展示语义与 backend/config.py 一致：进程环境变量优先于 .env（load_dotenv
override=False）。密钥字段只显示打码值，永不明文进入 TUI 或日志。
"""

from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

FIELDS = (
    "AGENT_INTENT_MODE",
    "LLM_PROVIDER",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "AGENT_BRIDGE_TOKEN",
)
SECRET_FIELDS = {"LLM_API_KEY", "AGENT_BRIDGE_TOKEN"}


def _read_env_file(project_root: Path) -> dict[str, str]:
    path = project_root / ".env"
    if not path.is_file():
        return {}
    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}
    values = dotenv_values(path) or {}
    return {k: v for k, v in values.items() if isinstance(v, str)}


def effective_values(project_root: Path, environ: dict | None = None) -> dict[str, str]:
    """合并进程环境变量与 .env（进程变量优先），只返回 FIELDS 内的字段。"""
    env = dict(os.environ if environ is None else environ)
    file_values = _read_env_file(project_root)
    merged = {k: v for k, v in file_values.items() if k in FIELDS and v != ""}
    for name in FIELDS:
        if name in env:
            merged[name] = env[name]
    return merged


def mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return "未设置"
    tail = value[-4:]
    return f"已设置（****{tail}）" if tail != value else "已设置（****）"


def load_env_overview(project_root: Path, environ: dict | None = None) -> list[tuple[str, str, str]]:
    """返回 (字段, 展示值, 来源) 行；来源为 进程环境变量/.env/未设置。"""
    env = dict(os.environ if environ is None else environ)
    file_values = _read_env_file(project_root)
    rows = []
    for name in FIELDS:
        if name in env:
            raw, source = env[name], "进程环境变量"
        elif file_values.get(name):
            raw, source = file_values[name], ".env"
        else:
            raw, source = "", "未设置"
        if name in SECRET_FIELDS:
            display = mask_secret(raw)
        else:
            display = raw if raw else "（未设置）"
        rows.append((name, display, source))
    return rows


def needs_setup(values: dict[str, str]) -> bool:
    """API 向导触发条件：缺 LLM_API_KEY。已有完整 .env 的用户不触发（零二次配置）。"""
    return not (values.get("LLM_API_KEY") or "").strip()


def required_for_mode(mode: str) -> list[str]:
    if mode == "llm":
        return [
            "LLM_API_KEY",
            "LLM_PROVIDER",
            "LLM_BASE_URL",
            "LLM_MODEL",
            "AGENT_BRIDGE_TOKEN",
        ]
    return []


def generate_bridge_token() -> str:
    return secrets.token_hex(16)


def write_env_file(path: Path, updates: dict[str, str], backup: bool = True) -> Path | None:
    """把 updates 合并写入 .env：保留原注释与未涉及键，更新已有键，追加新键。

    已存在文件先备份为 .env.bak-<时间戳>；返回备份路径（无备份返回 None）。
    """
    backup_path: Path | None = None
    original_lines: list[str] = []
    if path.is_file():
        original_lines = path.read_text(encoding="utf-8").splitlines()
        if backup:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup_path = path.with_name(f".env.bak-{stamp}")
            backup_path.write_text("\n".join(original_lines) + "\n", encoding="utf-8")
    output: list[str] = []
    written: set[str] = set()
    for line in original_lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                written.add(key)
                continue
        output.append(line)
    for key, value in updates.items():
        if key not in written and value != "":
            output.append(f"{key}={value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")
    return backup_path
