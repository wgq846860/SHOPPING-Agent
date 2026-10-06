"""生成启动器图标与快捷方式（LAUNCHER-1 补充，2026-10-06）。

- 把 frontend/img/agent-icon.png 包装成 Windows ICO（PNG-in-ICO，Vista+ 支持），
  输出 frontend/img/agent.ico；不引入 Pillow 等新依赖。
- 用 PowerShell WScript.Shell COM 创建根目录「启动器.lnk」，指向 启动器.bat
  并使用上述图标；可重复运行（覆盖旧 lnk）。
"""

from __future__ import annotations

import struct
import subprocess
from pathlib import Path

from .paths import PROJECT_ROOT

SOURCE_PNG = "frontend/img/agent-icon.png"
TARGET_ICO = "frontend/img/agent.ico"
BAT_NAME = "启动器.bat"
SHORTCUT_NAME = "启动器.lnk"


def wrap_png_as_ico(png: bytes, width: int, height: int) -> bytes:
    """单条目 ICO：ICONDIR + ICONDIRENTRY + 原始 PNG 数据。"""
    if not (0 < width <= 256 and 0 < height <= 256):
        raise ValueError("PNG 尺寸须在 1–256 之间（ICO 上限 256）")
    if png[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG 文件")
    header = struct.pack("<HHH", 0, 1, 1)  # reserved, type=icon, count=1
    entry = struct.pack(
        "<BBBBHHII",
        0 if width >= 256 else width,
        0 if height >= 256 else height,
        0,  # 调色板数
        0,  # 保留
        1,  # 色彩平面
        32,  # 位深
        len(png),
        6 + 16,  # 数据偏移
    )
    return header + entry + png


def build_icon(source: Path, target: Path) -> None:
    png = source.read_bytes()
    width, height = struct.unpack(">II", png[16:24])
    target.write_bytes(wrap_png_as_ico(png, width, height))


def create_shortcut(project_root: Path) -> Path:
    shortcut = project_root / SHORTCUT_NAME
    command = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$lnk = $ws.CreateShortcut('{shortcut}'); "
        f"$lnk.TargetPath = '{project_root / BAT_NAME}'; "
        f"$lnk.WorkingDirectory = '{project_root}'; "
        f"$lnk.IconLocation = '{project_root / TARGET_ICO},0'; "
        "$lnk.Description = 'Agentic Shopping Demo TUI 启动器'; "
        "$lnk.Save()"
    )
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", command],
        check=True,
        capture_output=True,
        timeout=60,
    )
    return shortcut


def main() -> None:
    build_icon(PROJECT_ROOT / SOURCE_PNG, PROJECT_ROOT / TARGET_ICO)
    shortcut = create_shortcut(PROJECT_ROOT)
    print(f"icon: {PROJECT_ROOT / TARGET_ICO}")
    print(f"shortcut: {shortcut}")


if __name__ == "__main__":
    main()
