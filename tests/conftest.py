"""共享 fixture：真实 Settings + 基于应用工厂的 TestClient。

测试不启动 Harness；数据/数据库类测试通过临时目录生成与导入，
不污染项目 data/db/。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.config import Settings, load_settings
from backend.main import create_app


@pytest.fixture()
def settings() -> Settings:
    """默认配置（锚定项目根的真实 frontend/ 目录）。"""
    return load_settings()


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    """S0.3 最小服务的测试客户端。"""
    return TestClient(create_app(settings))


@pytest.fixture(scope="session")
def small_data_dir(tmp_path_factory):
    """生成一次 small-v1 数据目录，供数据/校验/数据库测试共用。"""
    from scripts.generate_demo_data import DEFAULT_SEED, generate_profile

    data_dir = tmp_path_factory.mktemp("small_v1")
    generate_profile("small", data_dir, DEFAULT_SEED)
    return data_dir
