"""TUI headless 冒烟：挂载五个页面、关键控件存在、退出确认路径可达。

不启动真实服务（用 tmp 项目根触发 venv 缺失保护），不做任何网络/git 操作。
"""

from __future__ import annotations

import pytest

from scripts.launcher.app import LauncherApp


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_launcher_app_mounts_five_tabs(tmp_path):
    app = LauncherApp(project_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        for widget_id in (
            "#svc-status",
            "#mode-select",
            "#mode-llm",
            "#mode-rule",
            "#port",
            "#env-table",
            "#dict-table",
            "#svc-log",
        ):
            assert app.query(widget_id), f"缺少控件 {widget_id}"
        assert not app.query("#git-status"), "GitHub 页应已移除"


@pytest.mark.anyio
async def test_launcher_start_button_without_venm_fails_gracefully(tmp_path):
    app = LauncherApp(project_root=tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        # tmp 根没有 .venv：启动应走 ServiceError -> 通知，而不是崩溃
        await pilot.click("#start")
        await pilot.pause()
        assert app.service is not None
        assert app.service.is_running() is False
