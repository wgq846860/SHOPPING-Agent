"""Public Demo implementation."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"


def test_esc_helper_escapes_html_contexts():
    """esc() 必须转义 & < > " '，覆盖文本与属性上下文。"""
    import json

    node = shutil.which("node")
    if node is None:
        pytest.skip("未安装 Node，跳过 esc 行为单测")
    cases = [
        ["<i id=x>audit</i>", "&lt;i id=x&gt;audit&lt;/i&gt;"],
        ['"q"', "&quot;q&quot;"],
        ["a'b", "a&#39;b"],
        ["&amp;", "&amp;amp;"],
        ["<img src=x onerror=y>", "&lt;img src=x onerror=y&gt;"],
        [None, ""],
    ]
    script = (
        "const { esc } = require(process.argv[1]);"
        f"const cases = {json.dumps(cases)};"
        "for (const [input, expected] of cases) {"
        "  const got = esc(input);"
        "  if (got !== expected) { console.error('FAIL', input, got); process.exit(1); }"
        "}"
        "console.log('esc OK');"
    )
    proc = subprocess.run(
        [node, "-e", script, str(APP_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"esc 单测失败: {proc.stderr}"
    assert "esc OK" in proc.stdout


def test_audited_sinks_stay_fixed():
    """审计点名的三个注入 sink 必须保持修复形态（DOM API 或 esc()）。

    注意：本守卫锚定"出口形态"而非"中间拼串"——允许先组串、在进
    innerHTML/属性处统一 esc() 的写法；若回退为把 URL/API 文本直插
    innerHTML，下列断言会失败。
    """
    src = APP_JS.read_text(encoding="utf-8")
    assert "function esc(" in src, "esc() 转义函数缺失"

    # 1) 结果计数（原 count.innerHTML 直插 params.q）→ DOM API
    assert "count.innerHTML" not in src
    assert "qStrong.textContent = params.q" in src
    # 2) 筛选标签（原 chips.innerHTML 直插 q/brand/sub_category）→ DOM API
    assert "chips.innerHTML" not in src
    assert "chip.textContent = label" in src
    assert "btn.textContent = \"×\"" in src
    # 3) 详情错误分支（原直插 URL id）→ esc()
    assert "商品编号 ${id}" not in src
    assert "商品编号 ${esc(id)} 不存在" in src

    # API/数据库文本进 HTML 模板的出口必须经 esc()
    for wrapped in (
        "esc(summary.brand)",
        "esc(summary.product_name)",
        "esc(offer.merchant_name)",
        "esc(m.merchant_name)",
        "esc(MERCHANT_TYPE_LABELS[m.merchant_type]",
        "esc(product.brand)",
        "esc(product.product_name)",
        "esc(product.description)",
        "<td>${esc(v)}</td>",   # 商品参数表逐行出口（含 model/brand 等）
        "esc(view.alt)",
        "esc(view.image)",
        "esc(err.message)",
        "esc(b)",
        "esc(f)",
    ):
        assert wrapped in src, f"缺少转义出口: {wrapped}"
