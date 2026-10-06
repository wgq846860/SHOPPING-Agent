"""同一公开请求与购物工具的薄适配：服务端保存权威决策，不信任模型回显。"""
from __future__ import annotations

import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from backend.agent.harness_client import HarnessTimeoutError, HarnessUnavailableError, is_failed_finish_reason
from backend.agent.intent_parser import IntentParseError
from backend.agent.shopping_agent import PreparedDecision, prepare_decision
from backend.schemas.agent import ShoppingIntent


class DecisionBindingError(ValueError):
    pass


@dataclass
class _Pending:
    intent: ShoppingIntent
    now: datetime
    record: PreparedDecision | None = None


class DecisionRecords:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    @contextmanager
    def request(self, intent, now):
        key = uuid.uuid4().hex
        with self._lock:
            self._pending[key] = _Pending(intent.model_copy(deep=True), now)
        try:
            yield key
        finally:
            with self._lock:
                self._pending.pop(key, None)

    def prepare(self, key, intent, conn) -> PreparedDecision:
        with self._lock:
            pending = self._pending.get(key)
            if pending is None:
                raise DecisionBindingError("决策请求不存在或已结束")
            if intent.model_dump(mode="json") != pending.intent.model_dump(mode="json"):
                raise DecisionBindingError("工具意图与本次已校验意图不一致")
            if pending.record is None:
                pending.record = prepare_decision(conn, pending.intent, mode="llm", now=pending.now)
            return pending.record

    def run(self, intent, now, harness) -> PreparedDecision:
        with self.request(intent, now) as key:
            arguments = {"decision_request_id": key, "intent": intent.model_dump(mode="json")}
            prompt = (
                "请调用购物工具 recommend_from_intent，严格原样使用以下参数。"
                "decision_request_id 绑定当前请求，不能省略、改变意图或以文字代替调用。"
                "工具返回后结束回合，不生成推荐或解释文本。\n<tool_request>\n"
                + json.dumps(arguments, ensure_ascii=False) + "\n</tool_request>"
            )
            try:
                result = harness.run_intent_turn(prompt)
            except HarnessTimeoutError as exc:
                raise IntentParseError("timeout", "购物工具回合超时，请重试") from exc
            except HarnessUnavailableError as exc:
                raise IntentParseError("unavailable", "购物工具服务不可用") from exc
            if is_failed_finish_reason(result.finish_reason):
                raise IntentParseError("unavailable", "购物工具回合未完成")
            with self._lock:
                record = self._pending[key].record
            if record is None:
                raise IntentParseError("invalid_output", "本次请求未取得购物工具决策记录",
                                       [{"field": "query", "reason": "missing_decision_tool"}])
            return record
