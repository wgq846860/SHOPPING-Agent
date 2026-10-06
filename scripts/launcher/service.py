"""uvicorn 子进程托管（LAUNCHER-1）。

要点：
- 模式切换 = 把 AGENT_INTENT_MODE 注入子进程环境变量。backend/config.py 用
  load_dotenv(override=False)，进程环境变量优先于 .env，因此注入即可生效，
  永不修改 .env 文件。
- 子进程 stdout/stderr 追加写入 .runtime/launcher/uvicorn.log，由跟随线程
  读回 TUI 日志页；用文件而非管道，是为了在"TUI 退出但保持服务运行"时子进程
  写日志不因管道断裂而崩溃。
- 停止用 taskkill /T /F 杀进程树（覆盖 llm 模式下 harness runtime 的孙进程）。
- 启动前检测端口占用：只提示占用 PID，不代杀非本启动器管理的进程（README 规则）。
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.request
from collections import deque
from pathlib import Path

from .paths import venv_python

MODES = ("llm", "rule_based_demo")
DEFAULT_PORT = 8000
LOG_FILENAME = ".runtime/launcher/uvicorn.log"


class ServiceError(RuntimeError):
    """面向用户的启动/停止错误信息。"""


def build_child_env(mode: str, base: dict | None = None) -> dict[str, str]:
    """构造 uvicorn 子进程环境：base（缺省为当前进程）+ 模式注入。

    会话级注入：AGENT_INTENT_MODE 只影响本次子进程，不落 .env。
    """
    if mode not in MODES:
        raise ValueError(f"未知模式 {mode!r}；可选：{MODES}")
    env = dict(os.environ if base is None else base)
    env["AGENT_INTENT_MODE"] = mode
    env["PYTHONUTF8"] = "1"
    return env


def parse_netstat_listening(text: str, port: int) -> list[int]:
    """从 `netstat -ano -p tcp` 输出解析监听指定端口的 PID（去重、升序）。"""
    pids: set[int] = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP" or parts[3].upper() != "LISTENING":
            continue
        local_address = parts[1]
        if local_address.rsplit(":", 1)[-1] == str(port):
            try:
                pids.add(int(parts[4]))
            except ValueError:
                continue
    return sorted(pids)


def find_port_pids(port: int) -> list[int]:
    """查询本机监听该端口的进程 PID；netstat 不可用时返回空列表。"""
    try:
        proc = subprocess.run(
            ["netstat", "-ano", "-p", "tcp"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    return parse_netstat_listening(proc.stdout, port)


def fetch_health(port: int, timeout: float = 1.5) -> dict | None:
    """GET /api/health；服务未就绪/不可达返回 None，不抛异常。"""
    url = f"http://127.0.0.1:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


class ServiceManager:
    """单个 uvicorn 服务实例的生命周期管理。"""

    def __init__(self, project_root: Path, on_log=None, history: int = 400):
        self.project_root = project_root
        self.on_log = on_log
        self._history = history
        self._buffer: deque[str] = deque(maxlen=history)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # -- 状态 ------------------------------------------------------------
    @property
    def pid(self) -> int | None:
        proc = self._proc
        return proc.pid if proc is not None else None

    def is_running(self) -> bool:
        proc = self._proc
        return proc is not None and proc.poll() is None

    def tail(self, lines: int = 100) -> list[str]:
        return list(self._buffer)[-lines:]

    # -- 启停 ------------------------------------------------------------
    def start(self, mode: str, port: int = DEFAULT_PORT) -> int:
        if mode not in MODES:
            raise ServiceError(f"未知模式 {mode!r}")
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise ServiceError("服务已在运行，请先停止再启动")
        python = venv_python(self.project_root)
        if not python.is_file():
            raise ServiceError(
                f"未找到 {python}。请先按 README 创建 .venv 并安装依赖："
                ".venv\\Scripts\\python.exe -m pip install -r requirements.txt"
            )
        pids = find_port_pids(port)
        own = self.pid
        if pids and own not in pids:
            raise ServiceError(
                f"端口 {port} 已被其他进程占用（PID {pids}）。"
                "请先停止该进程或更换端口；启动器不代杀非本启动器管理的进程。"
            )
        log_path = self.project_root / LOG_FILENAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_file = log_path.open("ab")
        command = [
            str(python),
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]
        try:
            proc = subprocess.Popen(
                command,
                cwd=str(self.project_root),
                env=build_child_env(mode),
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            log_file.close()
            raise ServiceError(f"启动失败：{exc}") from exc
        with self._lock:
            self._proc = proc
        threading.Thread(
            target=self._replay_history, args=(log_path,), daemon=True
        ).start()
        threading.Thread(target=self._follow, args=(log_path, proc), daemon=True).start()
        return proc.pid

    def stop(self, timeout: float = 15.0) -> bool:
        """停止服务；返回是否真的停止了一个运行中的进程。"""
        with self._lock:
            proc = self._proc
            self._proc = None
        if proc is None:
            return False
        was_alive = proc.poll() is None
        if was_alive:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
            else:
                proc.kill()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                pass
        return was_alive

    # -- 日志跟随 ----------------------------------------------------------
    def _emit(self, line: str) -> None:
        self._buffer.append(line)
        if self.on_log is not None:
            try:
                self.on_log(line)
            except Exception:
                # 回调（TUI 已卸载等）失败不影响跟随线程
                pass

    def _replay_history(self, log_path: Path) -> None:
        try:
            data = log_path.read_bytes()
        except OSError:
            return
        lines = data.decode("utf-8", "replace").splitlines()
        for line in lines[-80:]:
            self._emit(line)

    def _follow(self, log_path: Path, proc: subprocess.Popen) -> None:
        """从文件末尾跟随新增日志；进程退出并冲刷完后结束线程。"""
        try:
            with log_path.open("rb") as handle:
                handle.seek(0, 2)
                while True:
                    line = handle.readline()
                    if line:
                        self._emit(line.decode("utf-8", "replace").rstrip("\r\n"))
                        continue
                    if proc.poll() is not None:
                        time.sleep(0.3)
                        rest = handle.read()
                        if rest:
                            for extra in rest.decode("utf-8", "replace").splitlines():
                                self._emit(extra)
                        return
                    time.sleep(0.3)
        except OSError:
            return
