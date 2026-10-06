"""最小 HTTP 服务测试：页面、静态资源、健康检查、未实现接口的 501 语义。

断言尽量锚定结构性标记（元素 id、类名）与项目承诺的"模拟数据"标注，
避免页面文案微调导致测试脆弱。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend import __version__
from backend.config import load_settings
from backend.main import create_app


# ---- 页面 -------------------------------------------------------------------


def test_pages_served_with_structure(client):
    cases = [
        ("/", ["search-scope", "agent-mark", "模拟数据"]),
        ("/index.html", ["EarHub"]),
        ("/product.html", ["EarHub", "detail-mount", "wireless_earbuds"]),
        ("/agent-shopping.html", ['id="agent-query"', "示例需求"]),
    ]
    for path, markers in cases:
        resp = client.get(path)
        assert resp.status_code == 200, path
        for marker in markers:
            assert marker in resp.text, (path, marker)


def test_pages_disable_cache(client):
    resp = client.get("/")
    assert resp.headers.get("cache-control") == "no-store"


def test_static_assets_disable_cache(client):
    # 本地演示站点改完 CSS/JS 刷新即生效：静态资源与页面一样禁缓存
    resp = client.get("/static/css/main.css")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-store"


def test_missing_frontend_file_returns_500(tmp_path):
    app = create_app(load_settings(frontend_dir=tmp_path))
    resp = TestClient(app).get("/")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "static_missing"


# ---- 静态资源 ---------------------------------------------------------------


def test_static_assets_served(client):
    for path in (
        "/static/css/main.css",
        "/static/js/app.js",
        "/static/img/agent-mark.png",
        "/static/img/agent-icon.png",
        "/static/img/p001.jpg",
        "/static/img/p012.jpg",
        "/static/img/p015.png",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert int(resp.headers.get("content-length", 0)) > 0


def test_product_detail_page_exposes_offer_facts_and_source_mounts(client):
    """S4.2 页面必须提供报价身份、配送/售后区域和来源信息入口。"""
    html = client.get("/product.html").text
    app_js = client.get("/static/js/app.js").text
    for marker in (
        "offer_id", "mode_id", "delivery_window", "effective_return_policy",
        "effective_warranty", "pdp-sources", "field_sources",
    ):
        assert marker in html or marker in app_js, marker
    assert 'p.days === 0 ? "不接受退货"' in app_js
    assert 'w.months === 0 ? "不提供保修"' in app_js
    assert '["续航", batteryText(product)]' in app_js
    assert '["评分", metricsText(metrics)]' in app_js


def test_static_directory_traversal_blocked(client):
    # StaticFiles 自带路径安全检查；构造越界路径不得读到项目文件
    resp = client.get("/static/../backend/config.py")
    assert resp.status_code in (400, 404)


# ---- 健康检查 ---------------------------------------------------------------


def test_health_reports_honest_capability(tmp_path, monkeypatch):
    """数据库未建时（隔离 data_dir），database 检查如实为 false。

    Agent 检查按实际意图模式上报：显式钉住环境（无密钥 → 规则演示 →
    agent_harness false），避免随用户本地 .env 漂移。
    """
    from backend.config import load_settings

    monkeypatch.delenv("AGENT_INTENT_MODE", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    isolated = load_settings(data_dir=tmp_path)
    resp = TestClient(create_app(isolated)).get("/api/health")
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "ok"
    assert body["stage"] == "S3.3"
    assert body["version"] == __version__
    assert body["request_id"].startswith("req_")

    meta = body["meta"]
    assert meta["api_version"] == "api.v0.1"
    assert meta["schema_version"] == "commerce.v0.1"
    assert meta["simulated"] is True

    checks = body["checks"]
    assert checks["static_frontend"]["ok"] is True
    # 健康检查必须如实上报，而不是猜
    assert checks["database"]["ok"] is False
    assert checks["ranking_engine"]["ok"] is False  # 无库时排序引擎不可用
    assert checks["agent_harness"]["ok"] is False
    assert "rule_based_demo" in checks["agent_harness"]["detail"]

    assert body["endpoints"]["implemented"] == [
        "GET /api/health",
        "GET /api/products",
        "GET /api/products/{id}",
        "GET /api/products/{id}/offers",
        "GET /api/merchants/{id}",
        "POST /api/agent-shopping",
    ]
    assert body["endpoints"]["planned"] == []


def test_health_agent_check_follows_llm_config(tmp_path, monkeypatch):
    """密钥就绪且未显式降级时，agent_harness 如实上报 llm 就绪。"""
    from backend.config import load_settings

    monkeypatch.delenv("AGENT_INTENT_MODE", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.setenv("AGENT_BRIDGE_TOKEN", "test-bridge-not-real")
    isolated = load_settings(data_dir=tmp_path)
    checks = TestClient(create_app(isolated)).get("/api/health").json()["checks"]
    assert checks["agent_harness"]["ok"] is True


def test_health_llm_missing_bridge_is_not_ready(tmp_path, monkeypatch):
    from backend.config import load_settings

    monkeypatch.setenv("AGENT_INTENT_MODE", "llm")
    monkeypatch.setenv("LLM_API_KEY", "test-key-not-real")
    monkeypatch.delenv("AGENT_BRIDGE_TOKEN", raising=False)
    checks = TestClient(create_app(load_settings(data_dir=tmp_path))).get("/api/health").json()["checks"]
    assert checks["agent_harness"]["ok"] is False


def test_health_database_check_follows_actual_file(tmp_path):
    """database.ok 与 db_ready() 一致：库文件存在后如实翻 true。"""
    from backend.config import load_settings

    isolated = load_settings(data_dir=tmp_path)
    assert isolated.db_ready() is False
    isolated.db_path.parent.mkdir(parents=True, exist_ok=True)
    isolated.db_path.write_bytes(b"")  # S1.2 后由 init_database.py 真实导入
    resp = TestClient(create_app(isolated)).get("/api/health")
    checks = resp.json()["checks"]
    assert checks["database"]["ok"] is True


# ---- 未实现 API：501 语义 ----------------------------------------------------


def test_unimplemented_api_returns_501(client):
    # S1.3 起 /api/products、S3.3 起 /api/agent-shopping 已实现；
    # 其余未实现路径保留 501 语义
    resp = client.get("/api/products/P001/reviews")
    assert resp.status_code == 501
    assert resp.json()["error"]["code"] == "not_implemented"
    assert resp.json()["error"]["details"]["stage"] == "S3.3"


def test_agent_shopping_route_registered(client):
    """S3.3 起 /api/agent-shopping 有专用 POST 路由，不再是 501 兜底占位。"""
    schema = client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/agent-shopping" in paths
    assert "post" in paths["/api/agent-shopping"]
    assert "post" not in paths.get("/api/products/P001/reviews", {})
