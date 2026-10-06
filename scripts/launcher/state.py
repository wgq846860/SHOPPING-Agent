"""启动器自身状态：存 .runtime/launcher/config.json（已被 .gitignore 排除，不上云）。

只保存非敏感的便利项：GitHub remote 地址、端口偏好、上次模式。
"""

from __future__ import annotations

import json
from pathlib import Path

from .service import MODES

DEFAULT_PORT = 8000
DEFAULT_STATE = {"port": DEFAULT_PORT, "last_mode": ""}


def state_path(project_root: Path) -> Path:
    return project_root / ".runtime" / "launcher" / "config.json"


def load_state(project_root: Path) -> dict:
    data: dict = {}
    try:
        path = state_path(project_root)
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    except (OSError, ValueError):
        data = {}
    result = dict(DEFAULT_STATE)
    port = data.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535:
        result["port"] = port
    mode = data.get("last_mode")
    if mode in MODES:
        result["last_mode"] = mode
    return result


def save_state(project_root: Path, **updates) -> dict:
    """读-改-写合并；未知键与非法值被丢弃，损坏文件回退默认。"""
    result = load_state(project_root)
    for key, value in updates.items():
        if key not in result:
            continue
        if key == "port":
            if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 65535:
                result["port"] = value
        elif key == "last_mode":
            if value in MODES:
                result["last_mode"] = value
    path = state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
