"""数据字典解析测试：合成样例 + 真实字典回归。"""

from __future__ import annotations

from scripts.launcher.fields import parse_data_dictionary
from scripts.launcher.paths import PROJECT_ROOT

SAMPLE = """# 数据字典

前言文字。

## 1. 商家

说明文字。

| 字段 | 类型/必填 |
| --- | --- |
| merchant_id | string/是 |
| region | string/可空 |

### 1.1 结构

| 结构.字段 | 类型 |
| --- | --- |
| `X.y` | int |

## 2. 无表格

正文规则，无字段表。
"""


def test_parse_sections_and_fields():
    sections = parse_data_dictionary(SAMPLE)
    assert [section.title for section in sections] == ["1. 商家", "1.1 结构", "2. 无表格"]
    assert [entry.name for entry in sections[0].fields] == ["merchant_id", "region"]
    assert sections[0].fields[0].meta == "string/是"
    assert [entry.name for entry in sections[1].fields] == ["X.y"]
    assert sections[2].fields == []


def test_parse_real_data_dictionary():
    path = PROJECT_ROOT / "docs" / "data_dictionary.md"
    sections = parse_data_dictionary(path.read_text(encoding="utf-8"))
    assert len(sections) >= 8
    product = next(section for section in sections if section.title.startswith("3. Product"))
    names = {entry.name for entry in product.fields}
    assert {"product_id", "connection_modes", "battery_specs", "codec_support"} <= names
    metrics = next(section for section in sections if section.title.startswith("4. ProductMetrics"))
    assert len(metrics.fields) >= 15
