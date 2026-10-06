"""Browser-side product list query behavior."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PRODUCTS_JS = Path(__file__).resolve().parent.parent / "frontend" / "js" / "products.js"


def test_product_list_query_preserves_filters_and_uses_api_page_size():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required to exercise the browser-side list query module")
    if not PRODUCTS_JS.exists():
        pytest.fail("product list query module is not implemented")

    script = r"""
const assert = require('node:assert/strict');
const products = require(process.argv[1]);
const params = products.readParams(
  '?q=%20gaming%20&brand=Audio%20%26%20Co&sub_category=gaming_headsets' +
  '&min_price_cents=1000&max_price_cents=50000&min_rating=4.5' +
  '&sort=price_asc&page=2&tracking=ignored'
);
assert.equal(products.buildProductsUrl(params),
  '/api/products?q=gaming&brand=Audio+%26+Co&sub_category=gaming_headsets' +
  '&min_price_cents=1000&max_price_cents=50000&min_rating=4.5' +
  '&sort=price_asc&page=2&page_size=24');

const defaults = products.readParams('?q=%20%20&page=-5&unknown=value');
assert.equal(products.buildProductsUrl(defaults),
  '/api/products?sort=relevance&page=1&page_size=24');
"""
    proc = subprocess.run(
        [node, "-e", script, str(PRODUCTS_JS)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, f"product list query behavior failed: {proc.stderr}"
