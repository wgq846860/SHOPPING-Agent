"""启动器路径解析。scripts/launcher/paths.py -> 项目根为上两级。"""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def venv_python(project_root: Path | None = None) -> Path:
    """项目 venv 的 Python 解释器（本机 PATH 上的 python 可能是 LibreOffice 的）。"""
    root = project_root or PROJECT_ROOT
    return root / ".venv" / "Scripts" / "python.exe"
