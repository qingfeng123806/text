"""Agent 接口与回复契约。

AgentReply 刻意带上 rule_decisions：我们不只关心 Agent 说了什么，
更关心它"依据哪条规则做了这个决定"。没有理由码的决定，在审计里等于没发生。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..tools.base import ToolContext, ToolResult


class Action(str, Enum):
    """Agent 本轮采取的动作类型，评测层据此分类。"""
    ASK_ORDER_ID = "ASK_ORDER_ID"        # 追问订单号
    ASK_CLARIFY = "ASK_CLARIFY"          # 其他澄清
    REFUSE = "REFUSE"                    # 拒绝办理
    REFUND_SUCCEEDED = "REFUND_SUCCEEDED"
    REFUND_PENDING = "REFUND_PENDING"    # 结果未知，挂起
    REFUND_FAILED = "REFUND_FAILED"      # 明确失败，非拒绝
    INFO = "INFO"                        # 纯信息答复


@dataclass
class RuleDecision:
    """一次规则判定。审计的基本单元之一。"""
    rule_id: str
    rule_name: str
    matched: bool
    reason_code: str
    message: str
    turn: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "rule_name": self.rule_name,
            "matched": self.matched,
            "reason_code": self.reason_code,
            "message": self.message,
            "turn": self.turn,
        }


@dataclass
class AgentReply:
    text: str
    action: Action = Action.INFO
    tool_calls: list[ToolResult] = field(default_factory=list)
    rule_decisions: list[RuleDecision] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def moved_money(self) -> bool:
        return any(t.moved_money for t in self.tool_calls)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "action": self.action.value,
            "tool_calls": [t.to_dict() for t in self.tool_calls],
            "rule_decisions": [r.to_dict() for r in self.rule_decisions],
            "meta": self.meta,
            "moved_money": self.moved_money,
        }


class Agent(ABC):
    """Agent 接口。实现可以是规则引擎，也可以换成大模型。"""

    agent_id: str = "agent"
    display_name: str = "Agent"
    description: str = ""

    @abstractmethod
    def reset(self, ctx: ToolContext, user_id: str) -> None:
        """会话开始时调用，清空本会话的私有状态。"""
        raise NotImplementedError

    @abstractmethod
    def handle(self, message: str, ctx: ToolContext, turn: int = 1) -> AgentReply:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "description": self.description,
        }
