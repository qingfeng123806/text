"""大模型适配器：把 Agent 的"决策内核"做成可替换的。

设计原则 —— 换内核，不换评测。

规则引擎和大模型的差别只在于"这一步该做什么"是怎么想出来的；
至于调用工具、记录 trace、受不变式约束，两者必须完全一致。
所以这里把决策抽成一个窄接口 decide()，外面套同一个 LLMAgent 壳：
壳负责调工具、落审计、过护栏，模型只负责给建议。

这样做的好处是，同一份 20 条用例集可以同时跑规则内核和模型内核，
差异全部体现在评测报告里，而不是靠人工感觉"模型好像变差了"。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from ..agent.protocol import Action, Agent, AgentReply, RuleDecision
from ..agent.rules import RuleConfig, extract_order_id, rule
from ..domain.models import OrderStatus
from ..tools.base import ToolContext, ToolStatus


@dataclass
class Decision:
    """模型给出的建议。刻意做得很窄：只表达意图，不碰工具。"""
    action: str                      # ask_order_id / refund / refuse / info
    order_id: Optional[str] = None
    reason: str = ""
    raw: Optional[dict[str, Any]] = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Decision":
        return cls(action=str(d.get("action", "info")),
                   order_id=d.get("order_id"),
                   reason=str(d.get("reason", "")),
                   raw=d)


class LLMAdapter(ABC):
    @abstractmethod
    def decide(self, message: str, transcript: list[dict[str, str]],
               known_order_ids: list[str]) -> Decision:
        raise NotImplementedError


class OpenAICompatibleAdapter(LLMAdapter):
    """调用 OpenAI 兼容的 chat/completions 接口。

    用标准库 urllib 实现，不引入 SDK —— 没配 Key 时不影响项目其余部分运行。
    需要两个环境变量：OPENAI_API_KEY、（可选）OPENAI_BASE_URL。
    """

    def __init__(self, model: str = "gpt-4o-mini", timeout: int = 30,
                 base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.model = model
        self.timeout = timeout
        self.base_url = (base_url or os.getenv("OPENAI_BASE_URL")
                         or "https://api.openai.com/v1").rstrip("/")
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")

    _SYSTEM = (
        "你是退款客服助手。只允许输出 JSON，格式："
        '{"action":"ask_order_id|refund|refuse|info","order_id":"...","reason":"..."}。'
        "用户未提供订单号时必须 ask_order_id，不得猜测订单号。"
    )

    def decide(self, message: str, transcript: list[dict[str, str]],
               known_order_ids: list[str]) -> Decision:
        if not self.api_key:
            raise RuntimeError(
                "未配置 OPENAI_API_KEY，无法调用大模型适配器。"
                "如需在不联网的环境评测，请继续使用规则内核 refund-agent-v1。")

        messages: list[dict[str, str]] = [{"role": "system", "content": self._SYSTEM}]
        for t in transcript:
            messages.append({"role": "user", "content": t.get("user", "")})
            if t.get("agent"):
                messages.append({"role": "assistant", "content": t["agent"]})
        messages.append({"role": "user", "content": message})

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"大模型接口返回 {exc.code}: {exc.read()[:200]}") from exc

        content = body["choices"][0]["message"]["content"]
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            parsed = {"action": "info", "reason": f"模型未返回合法 JSON：{content[:120]}"}
        return Decision.from_dict(parsed)


class LLMAgent(Agent):
    """用大模型做决策、用确定性规则做护栏的退款 Agent。

    护栏是强制的、不可关闭的：模型说要退款，也要先过归属、幂等、
    超时挂起三道检查。这样即使模型 hallucinate 出一个订单号，
    资金安全依然由代码保证，而不是由概率保证。
    """

    agent_id = "refund-agent-llm"
    display_name = "退款客服 Agent（大模型内核 + 确定性护栏）"
    description = "决策来自大模型，资金安全由与规则内核相同的护栏保证"

    def __init__(self, adapter: LLMAdapter, config: RuleConfig | None = None):
        self.adapter = adapter
        self.config = config or RuleConfig()
        self._user_id = ""
        self._pending: dict[str, str] = {}
        self._transcript: list[dict[str, str]] = []

    def reset(self, ctx: ToolContext, user_id: str) -> None:
        self._user_id = user_id
        self._pending.clear()
        self._transcript.clear()

    def handle(self, message: str, ctx: ToolContext, turn: int = 1) -> AgentReply:
        decisions: list[RuleDecision] = []
        calls: list[Any] = []
        known = [o.order_id for o in ctx.store.all_orders()]

        def decide(rid: str, matched: bool, code: str, msg: str) -> None:
            decisions.append(RuleDecision(rid, rule(rid).name, matched, code, msg, turn))

        d = self.adapter.decide(message, self._transcript, known)

        # ---- 护栏 1：没有订单号不许退款 ----
        oid = d.order_id or extract_order_id(message, known)[0]
        if d.action == "refund" and not oid:
            decide("R1", True, "GUARD_NO_ORDER_ID",
                   "模型建议退款但未提供订单号，护栏拦截并改为追问")
            d = Decision(action="ask_order_id", reason="护栏：缺少订单号")

        # ---- 护栏 2：超时挂起中的订单不许重试 ----
        if d.action == "refund" and oid in self._pending:
            decide("R4", True, "GUARD_PENDING_NO_RETRY",
                   f"订单 {oid} 处于超时挂起状态，护栏拦截模型的重试建议")
            d = Decision(action="info", order_id=oid, reason="护栏：挂起中不得重试")

        if d.action == "ask_order_id":
            lst = ctx.tools["list_user_orders"].run(ctx, user_id=self._user_id)
            calls.append(lst)
            reply = AgentReply(
                text="为了帮您办理退款，请提供需要退款的订单号（形如 SO-20261001-0001）。",
                action=Action.ASK_ORDER_ID, tool_calls=calls, rule_decisions=decisions,
                meta={"llm_reason": d.reason, "guard": True})
            self._sync(message, reply.text)
            return reply

        if d.action == "refund" and oid:
            order = ctx.store.get(oid)
            # ---- 护栏 3：归属校验 ----
            if order is None:
                return self._fail(f"没有查到订单 {oid}。", message, calls, decisions)
            if order.user_id != self._user_id:
                decide("R2", True, "GUARD_CROSS_USER",
                       f"护栏拦截：订单 {oid} 不属于当前用户")
                self._sync(message, "拒绝：订单不属于您。")
                return AgentReply(
                    text=f"订单 {oid} 并非您名下的订单，我无法为您办理这笔退款。",
                    action=Action.REFUSE, tool_calls=calls, rule_decisions=decisions,
                    meta={"guard": True})
            # ---- 护栏 4：幂等 ----
            if ctx.ledger.has_successful_refund(oid) or \
                    order.status is OrderStatus.REFUNDED:
                decide("R3", True, "GUARD_DUPLICATE",
                       f"护栏拦截：订单 {oid} 已退款")
                self._sync(message, "拒绝：已退款。")
                return AgentReply(
                    text=f"订单 {oid} 此前已经完成退款，同一笔订单不能重复退款。",
                    action=Action.REFUSE, tool_calls=calls, rule_decisions=decisions,
                    meta={"guard": True})

            res = ctx.tools["refund_order"].run(ctx, order_id=oid,
                                                user_id=self._user_id,
                                                reason=d.reason or "用户申请退款")
            calls.append(res)
            reply = self._after_refund(oid, res, calls, decisions, turn)
            self._sync(message, reply.text)
            return reply

        text = d.reason or "好的，请问还有什么可以帮您？"
        reply = AgentReply(text=text, action=Action.INFO, tool_calls=calls,
                           rule_decisions=decisions,
                           meta={"llm_reason": d.reason})
        self._sync(message, text)
        return reply

    # ---------- 内部 ----------

    def _after_refund(self, oid: str, res: Any, calls: list[Any],
                      decisions: list[RuleDecision], turn: int) -> AgentReply:
        if res.timed_out:
            self._pending[oid] = "TIMEOUT"
            decisions.append(RuleDecision("R4", rule("R4").name, True,
                                          "GUARD_TIMEOUT_PENDING",
                                          f"订单 {oid} 退款超时，转入挂起", turn))
            return AgentReply(
                text=(f"订单 {oid} 的退款请求已提交但支付网关超时，结果尚未确认。"
                      f"我会保持挂起并在对账后通知您，请不要重复提交。"),
                action=Action.REFUND_PENDING, tool_calls=calls,
                rule_decisions=decisions, meta={"guard": True})
        if res.ok and res.moved_money:
            return AgentReply(
                text=f"已为您办理订单 {oid} 的退款。",
                action=Action.REFUND_SUCCEEDED, tool_calls=calls,
                rule_decisions=decisions, meta={"guard": True})
        if res.status is ToolStatus.DENIED:
            return AgentReply(text=f"这笔退款无法办理：{res.error}",
                              action=Action.REFUSE, tool_calls=calls,
                              rule_decisions=decisions, meta={"guard": True})
        return AgentReply(text=f"退款处理失败：{res.error or '支付网关异常'}。",
                          action=Action.REFUND_FAILED, tool_calls=calls,
                          rule_decisions=decisions, meta={"guard": True})

    def _fail(self, text: str, message: str, calls: list[Any],
              decisions: list[RuleDecision]) -> AgentReply:
        reply = AgentReply(text=text, action=Action.REFUND_FAILED,
                           tool_calls=calls, rule_decisions=decisions)
        self._sync(message, text)
        return reply

    def _sync(self, user_msg: str, agent_msg: str) -> None:
        self._transcript.append({"user": user_msg, "agent": agent_msg})

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["adapter"] = type(self.adapter).__name__
        return d
