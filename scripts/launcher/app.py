"""Agentic Shopping Demo 的 Textual TUI 启动器主界面（LAUNCHER-1）。

页面：控制台（服务托管）｜配置（生效配置概览+API向导）｜字段（数据字典浏览）
｜日志。边界：不改后端业务与 API 契约；模式切换是会话级 env 注入，.env 永不
被启动器改写（API 向导仅在用户明确保存时写入，且自动备份）；密钥只以打码
形式显示。Git 上传不属启动器职责（2026-10-06 用户裁定，用其它工具执行；
上云排除项由根目录 .gitignore 保证）。
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RadioButton,
    RadioSet,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from .config_view import (
    FIELDS,
    SECRET_FIELDS,
    effective_values,
    generate_bridge_token,
    load_env_overview,
    needs_setup,
    write_env_file,
)
from .fields import parse_data_dictionary
from .paths import PROJECT_ROOT
from .service import MODES, ServiceError, ServiceManager, fetch_health
from .state import load_state, save_state

MODE_LABELS = {"llm": "Agent 模式（llm：真实调用模型）", "rule_based_demo": "场景模式（rule_based_demo：离线规则演示）"}


class ExitConfirmScreen(ModalScreen):
    """退出确认：服务仍在运行时询问处置。"""

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.message)
            with Horizontal():
                yield Button("停止服务并退出", id="exit-stop", variant="error")
                yield Button("保持运行并退出", id="exit-keep", variant="success")
                yield Button("取消", id="exit-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.dismiss(event.button.id or "exit-cancel")


class ApiWizardScreen(ModalScreen):
    """API 配置向导：仅在用户显式打开或保存时才写 .env（写前自动备份）。"""

    def __init__(self, prefills: dict[str, str]) -> None:
        super().__init__()
        self.prefills = prefills

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(
                "API 配置向导 — 保存时写入 .env（原文件自动备份）。\n"
                "留空的字段保持不变；已有完整配置无需重新填写。"
            )
            for name in FIELDS:
                placeholder = "随机生成" if name == "AGENT_BRIDGE_TOKEN" else ""
                yield Input(
                    value=self.prefills.get(name, ""),
                    id=f"env-{name}",
                    password=name in SECRET_FIELDS,
                    placeholder=placeholder,
                )
            with Horizontal():
                yield Button("保存到 .env", id="wiz-save", variant="success")
                yield Button("取消", id="wiz-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "wiz-save":
            updates: dict[str, str] = {}
            for name in FIELDS:
                value = self.query_one(f"#env-{name}", Input).value.strip()
                if value:
                    updates[name] = value
            if not updates.get("AGENT_BRIDGE_TOKEN") and "LLM_API_KEY" in updates:
                updates["AGENT_BRIDGE_TOKEN"] = generate_bridge_token()
            self.dismiss(updates)
        else:
            self.dismiss(None)


class LauncherApp(App):
    TITLE = "Agentic Shopping Demo 启动器"
    BINDINGS = [("ctrl+q", "quit", "退出（服务运行时会先询问）")]

    CSS = """
    #svc-status {
        height: auto;
        border: round $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    #mode-select { height: auto; margin-bottom: 1; }
    #buttons { height: auto; align: left middle; }
    #buttons Input { width: 24; margin: 0 1 0 0; }
    Button { margin: 0 1; }
    #svc-hint { color: $text-muted; height: auto; margin-top: 1; }
    #env-note { height: auto; margin-bottom: 1; }
    #dict-note { height: auto; color: $text-muted; }
    DataTable { height: 1fr; }
    RichLog { height: 1fr; border: round $secondary; }
    ModalScreen { align: center middle; }
    #dialog {
        width: 76;
        max-height: 80%;
        border: thick $primary;
        padding: 1 2;
        background: $surface;
    }
    #dialog Label { margin-bottom: 1; }
    #dialog Input { width: 100%; }
    #dialog Checkbox { margin: 1 0; }
    #dialog Horizontal { height: auto; align: right middle; }
    """

    def __init__(self, project_root: Path | None = None) -> None:
        super().__init__()
        self.project_root = project_root or PROJECT_ROOT
        self.service: ServiceManager | None = None
        self._browser_opened = False
        self._started_mode: str | None = None
        self._port_default = 8000

    # -- 布局 --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("控制台"):
                yield Static("", id="svc-status")
                yield RadioSet(
                    RadioButton(MODE_LABELS["llm"], id="mode-llm"),
                    RadioButton(MODE_LABELS["rule_based_demo"], id="mode-rule"),
                    id="mode-select",
                )
                with Horizontal(id="buttons"):
                    yield Input(placeholder="端口（默认 8000）", id="port")
                    yield Button("启动服务", id="start", variant="success")
                    yield Button("停止", id="stop", variant="error")
                    yield Button("重启", id="restart", variant="warning")
                    yield Button("打开页面", id="open")
                yield Static(
                    "模式为本次会话注入（不修改 .env）：Agent 模式需已配置密钥与工具桥令牌，"
                    "缺配时接口按 503 如实返回；场景模式离线可演示。数据为固定快照的模拟数据。",
                    id="svc-hint",
                )
            with TabPane("配置"):
                yield Static("", id="env-note")
                yield DataTable(id="env-table")
                with Horizontal():
                    yield Button("打开 API 配置向导", id="env-wizard")
            with TabPane("字段"):
                yield DataTable(id="dict-table")
                yield Static(
                    "来源：docs/data_dictionary.md（数据契约字段分组）。"
                    "意图（ShoppingIntent）契约不在本字典，见 docs/api_contract.md。",
                    id="dict-note",
                )
            with TabPane("日志"):
                yield RichLog(id="svc-log")
        yield Footer()

    # -- 挂载 ----------------------------------------------------------------
    def on_mount(self) -> None:
        self.service = ServiceManager(self.project_root, on_log=self._on_service_log)
        state = load_state(self.project_root)
        self._port_default = state["port"]
        self.query_one("#port", Input).value = str(state["port"])
        self._apply_default_mode(state)
        self._render_env()
        self._render_fields()
        self.set_interval(2.0, self._tick)

    def _apply_default_mode(self, state: dict) -> None:
        values = effective_values(self.project_root)
        preferred = state.get("last_mode") or ""
        if preferred not in MODES:
            preferred = (values.get("AGENT_INTENT_MODE") or "").strip()
        if preferred not in MODES:
            preferred = "llm" if (values.get("LLM_API_KEY") or "").strip() else "rule_based_demo"
        self.query_one("#mode-llm", RadioButton).value = preferred == "llm"
        self.query_one("#mode-rule", RadioButton).value = preferred == "rule_based_demo"

    # -- 控制台：状态与启停 ----------------------------------------------------
    def _current_port(self) -> int:
        raw = self.query_one("#port", Input).value.strip()
        try:
            port = int(raw)
        except ValueError:
            return self._port_default
        return port if 1 <= port <= 65535 else self._port_default

    def _selected_mode(self) -> str:
        for button in self.query_one("#mode-select", RadioSet).query(RadioButton):
            if button.value:
                return "llm" if button.id == "mode-llm" else "rule_based_demo"
        return "rule_based_demo"

    def _tick(self) -> None:
        if not self.is_mounted or self.service is None:
            return
        running = self.service.is_running()
        port = self._current_port()
        health = fetch_health(port) if running else None
        self._render_status(running, port, health)
        if running and health and not self._browser_opened:
            self._browser_opened = True
            webbrowser.open(f"http://127.0.0.1:{port}/")

    def _render_status(self, running: bool, port: int, health: dict | None) -> None:
        if running:
            mode = self._started_mode or "未知"
            line1 = (
                f"[b green]● 运行中[/]  PID {self.service.pid}  端口 {port}  "
                f"模式 [b]{mode}[/]（本次会话注入，未改 .env）"
            )
        else:
            line1 = "[b dim]○ 服务未运行[/]"
        if health:
            checks = health.get("checks", {})
            rendered = "  ".join(
                f"{name} {'[green]✓[/]' if item.get('ok') else '[red]✗[/]'}"
                for name, item in checks.items()
            )
            line2 = f"健康检查：{rendered or '（无检查项）'}"
        elif running:
            line2 = "健康检查：等待服务就绪…"
        else:
            line2 = "健康检查：—"
        self.query_one("#svc-status", Static).update(f"{line1}\n{line2}")

    def _start_service(self) -> None:
        if self.service is None:
            return
        mode = self._selected_mode()
        port = self._current_port()
        try:
            pid = self.service.start(mode, port)
        except ServiceError as exc:
            self.notify(str(exc), severity="error")
            return
        self._started_mode = mode
        self._browser_opened = False
        save_state(self.project_root, last_mode=mode, port=port)
        self.notify(f"已启动（PID {pid}，模式 {mode}）；就绪后将自动打开浏览器")

    def _stop_service(self, notify: bool = True) -> None:
        if self.service is None:
            return
        if self.service.stop():
            self._started_mode = None
            if notify:
                self.notify("服务已停止")
        elif notify:
            self.notify("服务未在运行")

    # -- 服务日志线程回调 -------------------------------------------------------
    def _on_service_log(self, line: str) -> None:
        try:
            self.call_from_thread(self._append_service_log, line)
        except Exception:
            # TUI 正在退出/未挂载时静默丢弃
            pass

    def _append_service_log(self, line: str) -> None:
        self.query_one("#svc-log", RichLog).write(line)

    # -- 配置页 ---------------------------------------------------------------
    def _render_env(self) -> None:
        table = self.query_one("#env-table", DataTable)
        table.clear(columns=True)
        table.add_columns("字段", "当前生效值", "来源")
        for field, display, source in load_env_overview(self.project_root):
            table.add_row(field, display, source)
        configured = not needs_setup(effective_values(self.project_root))
        note = self.query_one("#env-note", Static)
        if configured:
            note.update("[green]已配置 API 密钥（无需二次配置）[/]。来源：进程环境变量优先，其次 .env。")
        else:
            note.update(
                "[yellow]检测到未配置 LLM_API_KEY[/]：规则演示不受影响；"
                "要使用 Agent 模式请点下方向导补齐。"
            )

    def _open_api_wizard(self) -> None:
        prefills = effective_values(self.project_root)
        self.push_screen(ApiWizardScreen(prefills), self._on_wizard_done)

    def _on_wizard_done(self, updates: dict | None) -> None:
        if not updates:
            return
        backup = write_env_file(self.project_root / ".env", updates)
        message = "已写入 .env；重启服务后生效"
        if backup is not None:
            message += f"（备份：{backup.name}）"
        self.notify(message)
        self._render_env()

    # -- 字段页 ---------------------------------------------------------------
    def _render_fields(self) -> None:
        table = self.query_one("#dict-table", DataTable)
        table.clear(columns=True)
        table.add_columns("分组", "字段", "类型/必填")
        dictionary = self.project_root / "docs" / "data_dictionary.md"
        if not dictionary.is_file():
            table.add_row("（未找到 docs/data_dictionary.md）", "", "")
            return
        sections = parse_data_dictionary(dictionary.read_text(encoding="utf-8"))
        for section in sections:
            if not section.fields:
                table.add_row(section.title, "（本节为正文规则，无字段表）", "")
                continue
            for entry in section.fields:
                table.add_row(section.title, entry.name, entry.meta)

    # -- 事件分发 -----------------------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id
        if button_id == "start":
            self._start_service()
        elif button_id == "stop":
            self._stop_service()
        elif button_id == "restart":
            self._stop_service(notify=False)
            self._start_service()
        elif button_id == "open":
            webbrowser.open(f"http://127.0.0.1:{self._current_port()}/")
        elif button_id == "env-wizard":
            self._open_api_wizard()

    # -- 退出确认 -----------------------------------------------------------------
    def action_quit(self) -> None:
        if self.service is not None and self.service.is_running():
            self.push_screen(
                ExitConfirmScreen("服务仍在运行。退出启动器前要先停止服务吗？"),
                self._on_exit_confirm,
            )
        else:
            self.exit()

    def _on_exit_confirm(self, result: str | None) -> None:
        if result == "exit-stop":
            if self.service is not None:
                self.service.stop()
            self.exit()
        elif result == "exit-keep":
            # 服务日志走文件而非管道，子进程在启动器退出后可继续运行
            self.exit()


def main() -> None:
    LauncherApp().run()
