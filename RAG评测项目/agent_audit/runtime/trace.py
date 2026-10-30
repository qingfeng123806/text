"""Trace 数据结构：一次会话的完整行为轨迹。

设计取舍 —— 不记录"Agent 说了什么"就完事，而是把三层事实分开存：
  1. rule_decisions：Agent 声称自己依据了哪条规则
  2. tool_calls   ：Agent 实际调用了什么工具、传了什么、耗时多久、动没动钱
  3. ledger       ：账本最终认了什么

三层对不上，就是审计要盯的裂缝。比如 Agent 声称 REFUND_SUCCEEDED，
但账本里只有一条 TIMEOUT —— 那就是幻觉。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from ..agent.protocol import AgentReply
from ..tools.base import ToolResult


@dataclass
class Turn:
    index: int
    user_message: str
    reply: Optional[AgentReply] = None
    started_ms: int = 0
    ended_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "user_message": self.user_message,
            "reply": self.reply.to_dict() if self.reply else None,
            "started_ms": self.started_ms,
            "ended_ms": self.ended_ms,
            "duration_ms": self.ended_ms - self.started_ms,
        }


@dataclass
class Trace:
    trace_id: str
    run_id: str
    case_id: str
    case_title: str
    agent_id: str
    user_id: str
    turns: list[Turn] = field(default_factory=list)
    fault_specs: list[dict[str, Any]] = field(default_factory=list)
    orders_before: dict[str, Any] = field(default_factory=dict)
    orders_after: dict[str, Any] = field(default_factory=dict)
    ledger_snapshot: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    created_at: str = ""

    # ---------- 派生视图 ----------

    @property
    def all_tool_calls(self) -> list[ToolResult]:
        out: list[ToolResult] = []
        for t in self.turns:
            if t.reply:
                out.extend(t.reply.tool_calls)
        return out

    @property
    def all_rule_decisions(self) -> list[Any]:
        out = []
        for t in self.turns:
            if t.reply:
                out.extend(t.reply.rule_decisions)
        return out

    def count_tool(self, name: str, money_moved_only: bool = False) -> int:
        n = 0
        for c in self.all_tool_calls:
            if c.tool != name:
                continue
            if money_moved_only and not c.moved_money:
                continue
            n += 1
        return n

    def timeouts(self) -> list[ToolResult]:
        return [c for c in self.all_tool_calls if c.timed_out]

    def money_moves(self) -> list[ToolResult]:
        return [c for c in self.all_tool_calls if c.moved_money]

    def final_reply(self) -> Optional[AgentReply]:
        for t in reversed(self.turns):
            if t.reply is not None:
                return t.reply
        return None

    def transcript(self) -> list[dict[str, Any]]:
        return [{
            "turn": t.index,
            "user": t.user_message,
            "agent": t.reply.text if t.reply else "",
            "action": t.reply.action.value if t.reply else "",
        } for t in self.turns]

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "case_title": self.case_title,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "tags": self.tags,
            "created_at": self.created_at,
            "fault_specs": self.fault_specs,
            "turns": [t.to_dict() for t in self.turns],
            "orders_before": self.orders_before,
            "orders_after": self.orders_after,
            "ledger_snapshot": self.ledger_snapshot,
            "stats": {
                "tool_call_count": len(self.all_tool_calls),
                "money_move_count": len(self.money_moves()),
                "timeout_count": len(self.timeouts()),
                "rule_decision_count": len(self.all_rule_decisions),
            },
        }
