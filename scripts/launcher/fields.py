"""解析 docs/data_dictionary.md 为分组字段表（LAUNCHER-1 字段浏览页）。

只读展示，不复制字典语义；意图（ShoppingIntent）契约在 docs/api_contract.md，
本页页脚注明，不在此重复定义。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field


@dataclass
class FieldEntry:
    name: str
    meta: str = ""


@dataclass
class Section:
    title: str
    fields: list[FieldEntry] = dataclass_field(default_factory=list)


_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _split_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _table_fields(rows: list[list[str]]) -> list[FieldEntry]:
    """标准 markdown 表：首行表头、次行分隔线；其余为数据行（首列=字段名）。"""
    if len(rows) >= 2 and rows[1] and all(
        _SEPARATOR_CELL.match(cell) for cell in rows[1] if cell != ""
    ):
        data = rows[2:]
    elif rows:
        data = rows[1:]
    else:
        data = []
    entries: list[FieldEntry] = []
    for row in data:
        if not row or not row[0]:
            continue
        name = row[0].replace("`", "").strip()
        if name:
            entries.append(FieldEntry(name, row[1] if len(row) > 1 else ""))
    return entries


def parse_data_dictionary(text: str) -> list[Section]:
    """按 ## / ### 标题切段；段内 markdown 表的首列聚合为字段清单。"""
    sections: list[Section] = []
    current: Section | None = None
    table_rows: list[list[str]] = []

    def flush() -> None:
        nonlocal table_rows
        if current is not None and table_rows:
            current.fields.extend(_table_fields(table_rows))
        table_rows = []

    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("### ") and current is not None:
            flush()
            current = Section(line[4:].strip())
            sections.append(current)
            continue
        if line.startswith("## "):
            flush()
            current = Section(line[3:].strip())
            sections.append(current)
            continue
        stripped = line.strip()
        if stripped.startswith("|") and current is not None:
            table_rows.append(_split_cells(stripped))
            continue
        if table_rows:
            flush()
    flush()
    return sections
