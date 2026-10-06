"""Browser Agent submission behavior, exercised through Node's built-in runtime."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "agent.js"


def test_newest_agent_submission_is_the_only_result_rendered():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise Agent submission ordering")

    script = r"""
const assert = require('node:assert/strict');
let agent = {};
try {
  agent = require(process.argv[1]);
} catch (error) {
  if (error.code !== 'MODULE_NOT_FOUND' || !error.message.includes('agent.js')) throw error;
}
assert.equal(typeof agent.createSubmissionHandler, 'function',
  'the Agent page needs a testable submission handler');
assert.equal(typeof agent.normalizeApiError, 'function',
  'the Agent page needs to translate the API error envelope');
assert.equal(typeof agent.activeWeights, 'function',
  'the Agent page needs to distinguish active ranking weights');
assert.deepEqual(agent.activeWeights({ price: 0, battery: 0.5, commute: 0.5, unused: 0 }), [
  ['battery', 0.5], ['commute', 0.5]
]);
assert.deepEqual(agent.normalizeApiError({
  error: { code: 'LLM_TIMEOUT', message: '意图解析超时', retryable: true }
}, 504), {
  code: 'LLM_TIMEOUT', message: '意图解析超时', retryable: true, status: 504
});

const pending = new Map();
const rendered = [];
const errors = [];
const loading = [];
const requests = [];
const submit = agent.createSubmissionHandler({
  fetchImpl: (url, options) => {
    const query = JSON.parse(options.body).query;
    requests.push({ url, method: options.method, query });
    return new Promise(resolve => pending.set(query, resolve));
  },
  onLoading: value => loading.push(value),
  onSuccess: payload => rendered.push(payload),
  onError: message => errors.push(message)
});
const result = (name) => ({ ok: true, status: 200, json: async () => ({ status: 'ok', name }) });

(async () => {
  const older = submit('older request');
  const newer = submit('newer request');
  assert.deepEqual(requests, [
    { url: '/api/agent-shopping', method: 'POST', query: 'older request' },
    { url: '/api/agent-shopping', method: 'POST', query: 'newer request' }
  ]);
  pending.get('newer request')(result('newer'));
  await newer;
  pending.get('older request')(result('older'));
  await older;
  assert.deepEqual(rendered, [{ status: 'ok', name: 'newer' }]);
  assert.deepEqual(errors, []);
  assert.deepEqual(loading, [true, true, false]);

  const failureErrors = [];
  const submitFailure = agent.createSubmissionHandler({
    fetchImpl: async () => ({
      ok: false,
      status: 503,
      json: async () => ({ error: {
        code: 'LLM_UNAVAILABLE', message: '意图解析服务不可用', retryable: true
      } })
    }),
    onLoading: () => {},
    onSuccess: () => assert.fail('a 503 must not be rendered as a successful recommendation'),
    onError: error => failureErrors.push(error)
  });
  await submitFailure('service error');
  assert.deepEqual(failureErrors, [{
    code: 'LLM_UNAVAILABLE', message: '意图解析服务不可用', retryable: true, status: 503
  }]);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    proc = subprocess.run(
        [node, "-e", script, str(AGENT_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"Agent latest-submission behavior failed: {proc.stderr}"


def _render_agent_response(payload: dict) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise Agent response rendering")

    script = r"""
class Element {
  constructor(tag) {
    this.tag = tag;
    this.className = '';
    this.children = [];
    this.listeners = {};
    this.attributes = {};
    this._text = '';
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  get childElementCount() { return this.children.length; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  focus() {}
}

const ids = ['agent-form', 'agent-query', 'agent-status', 'example-list',
  'agent-result-content', 'agent-result-title', 'agent-result-mode'];
const elements = Object.fromEntries(ids.map(id => [id, new Element('div')]));
const doc = {
  readyState: 'complete',
  getElementById: id => elements[id],
  createElement: tag => new Element(tag),
};
const payload = JSON.parse(process.argv[2]);
global.document = doc;
global.window = {
  fetch: async () => ({ ok: true, status: 200, json: async () => payload }),
};
require(process.argv[1]);
elements['agent-query'].value = 'test query';
elements['agent-form'].listeners.submit({ preventDefault() {} });

function find(node, className) {
  const own = node.className.split(/\s+/).includes(className) ? [node] : [];
  return own.concat(node.children.flatMap(child => find(child, className)));
}

setImmediate(() => {
  const offers = find(elements['agent-result-content'], 'agent-offer');
  process.stdout.write(JSON.stringify({
    filters: find(elements['agent-result-content'], 'agent-filter-count').map(node => node.textContent),
    unknownCounts: find(elements['agent-result-content'], 'agent-filter-unknown').map(node => node.textContent),
    preferences: find(elements['agent-result-content'], 'agent-chip').map(node => node.textContent),
    offers: offers.map(offer => ({
      text: offer.textContent,
      detailHref: find(offer, 'agent-detail-link')[0]?.href,
    })),
  }));
});
"""
    proc = subprocess.run(
        [node, "-e", script, str(AGENT_JS), json.dumps(payload, ensure_ascii=False)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert proc.returncode == 0, f"Agent response rendering failed: {proc.stderr}"
    return json.loads(proc.stdout)


def _agent_response_fixture() -> dict:
    return {
        "status": "ok",
        "mode": "rule_based_demo",
        "intent": {
            "budget": {},
            "hard_constraints": {},
            "preferences": {"environment_awareness": 0.8},
            "clarifications": [],
        },
        "filters_applied": [{
            "field": "budget.max_cents",
            "operator": "lte",
            "value": 100000,
            "unit": "CNY_cent",
            "scope": "offer",
            "excluded_count": 22,
            "unknown_excluded_count": 3,
        }],
        "ranking_weights": {"product": {}, "offer": {}},
        "recommendations": [{
            "rank": 1,
            "score": 80,
            "product": {"product_id": "P009", "product_name": "Test headset", "brand": "Test"},
            "offer": {
                "product_id": "P009",
                "mode_id": "wireless-dongle",
                "merchant_name": "Test merchant",
                "landed_price_cents": 79900,
                "price_cents": 79900,
                "shipping_fee_cents": 0,
                "stock": 2,
                "delivery_window": {
                    "earliest": "2026-09-30",
                    "latest": "2026-10-01",
                    "commitment_type": "guaranteed",
                    "observed_at": "2026-09-29T10:00:00+08:00",
                },
                "effective_return_policy": {},
                "effective_warranty": {},
            },
            "score_breakdown": {"product": [], "offer": []},
            "reason": [],
            "strengths": [],
            "weaknesses": [],
            "trade_offs": [],
        }],
        "decision_summary": {"candidate_count": 1, "eligible_offer_count": 1, "returned_count": 1},
        "warnings": [],
        "meta": {"simulated": True},
    }


def test_agent_detail_link_preserves_the_recommended_connection_mode():
    rendered = _render_agent_response(_agent_response_fixture())

    assert rendered["offers"][0]["detailHref"] == (
        "/product.html?id=P009&mode_id=wireless-dongle"
    )


def test_agent_delivery_window_is_labeled_as_a_historical_observation():
    rendered = _render_agent_response(_agent_response_fixture())
    delivery = rendered["offers"][0]["text"]

    assert "2026-09-29" in delivery
    assert "历史" in delivery
    assert "不代表当前下单承诺" in delivery


def test_agent_unknown_delivery_commitment_is_not_shown_as_estimated():
    payload = _agent_response_fixture()
    payload["recommendations"][0]["offer"]["delivery_window"]["commitment_type"] = "unknown"
    rendered = _render_agent_response(payload)
    delivery = rendered["offers"][0]["text"]

    assert "历史配送 2026-09-30 至 2026-10-01" in delivery
    assert "历史预计" not in delivery


def test_agent_filter_counts_name_the_product_unit_and_scope_separately():
    rendered = _render_agent_response(_agent_response_fixture())

    assert "作用范围：报价" in rendered["filters"][0]
    assert "排除 22 个商品" in rendered["filters"][0]
    assert "其中 3 个商品因数据未知而排除" in rendered["unknownCounts"][0]


def test_agent_environment_awareness_uses_the_environment_sound_label():
    rendered = _render_agent_response(_agent_response_fixture())

    assert "环境音感知80%" in rendered["preferences"]
    assert "环保80%" not in rendered["preferences"]
