"""Detail-page fact formatters preserve spec applicability and nullable metrics."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_battery_displays_mode_and_test_conditions_for_specs_and_bullets():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the browser-side detail formatters")

    app_js = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"
    script = r"""
const assert = require('node:assert/strict');
const { batteryText, bulletList } = require(process.argv[1]);
const product = {
  connection_modes: [
    { mode_id: 'bt', transport: 'bluetooth' },
    { mode_id: 'dongle', transport: 'wireless_dongle' },
  ],
  battery_specs: [{
    hours: 35,
    basis: 'single_charge',
    anc_state: 'not_applicable',
    mode_id: 'dongle',
    test_conditions: '模拟50%音量听音，禁用灯效',
  }],
};
const expected = '35 小时（单次充电·ANC 不适用）；适用模式：无线发射器（dongle）；测试条件：模拟50%音量听音，禁用灯效';
assert.equal(batteryText(product), expected);
assert.ok(bulletList(product).includes(`续航：${expected}`));

const unknown = {
  connection_modes: [],
  battery_specs: [{
    hours: null,
    basis: 'single_charge',
    anc_state: 'unknown',
    mode_id: null,
    test_conditions: null,
  }],
};
assert.match(batteryText(unknown), /续航时长未知/);
assert.match(batteryText(unknown), /适用模式未知/);
assert.match(batteryText(unknown), /测试条件未知/);
"""
    proc = subprocess.run(
        [node, "-e", script, str(app_js)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"battery detail formatting failed: {proc.stderr}"


def test_rating_and_review_count_are_formatted_independently():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the browser-side detail formatters")

    app_js = Path(__file__).resolve().parent.parent / "frontend" / "js" / "app.js"
    script = r"""
const assert = require('node:assert/strict');
const { metricsText } = require(process.argv[1]);
assert.equal(metricsText({ rating: 4.5, review_count: null }), '4.5 分（评价数量未知）');
assert.equal(metricsText({ rating: null, review_count: 0 }), '评分未知（0 条评价）');
assert.equal(metricsText({ rating: null, review_count: null }), '评分未知（评价数量未知）');
"""
    proc = subprocess.run(
        [node, "-e", script, str(app_js)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"product metrics formatting failed: {proc.stderr}"
