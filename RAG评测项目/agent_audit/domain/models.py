"""领域数据模型。

这里的对象是审计系统的事实来源：Agent 说什么不算数，账本里记了什么才算数。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class OrderStatus(str, Enum):
    PAID = "PAID"
    REFUNDING = "REFUNDING"
    REFUNDED = "REFUNDED"
    CLOSED = "CLOSED"


class RefundOutcome(str, Enum):
    """一次退款尝试的最终结局。"""
    SUCCESS = "SUCCESS"
    REJECTED_DUPLICATE = "REJECTED_DUPLICATE"   # 已退过，拒绝重复退款
    REJECTED_NOT_OWNER = "REJECTED_NOT_OWNER"   # 订单不属于当前用户
    REJECTED_NOT_FOUND = "REJECTED_NOT_FOUND"
    REJECTED_NOT_REFUNDABLE = "REJECTED_NOT_REFUNDABLE"  # 订单状态不允许
    TIMEOUT = "TIMEOUT"           # 工具超时，结果未知(悬而未决)
    ERROR = "ERROR"


@dataclass
class Order:
    order_id: str
    user_id: str
    amount_cents: int
    item: str
    status: OrderStatus = OrderStatus.PAID
    created_at: str = ""

    @property
    def amount_yuan(self) -> float:
        return self.amount_cents / 100.0

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        d["amount_yuan"] = self.amount_yuan
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Order":
        return cls(
            order_id=d["order_id"],
            user_id=d["user_id"],
            amount_cents=int(d["amount_cents"]),
            item=d.get("item", ""),
            status=OrderStatus(d.get("status", "PAID")),
            created_at=d.get("created_at", ""),
        )


@dataclass
class RefundRecord:
    """退款账本中的一条流水。每一次真实的资金动作都会落一条。"""
    seq: int
    order_id: str
    user_id: str
    amount_cents: int
    outcome: RefundOutcome
    reason: str = ""
    timestamp: str = ""
    trace_id: str = ""
    attempt_index: int = 1      # 同一 trace 内对该订单的第几次尝试

    @property
    def is_money_moved(self) -> bool:
        """是否真的动了钱。只有 SUCCESS 算。"""
        return self.outcome is RefundOutcome.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["outcome"] = self.outcome.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RefundRecord":
        return cls(
            seq=int(d["seq"]),
            order_id=d["order_id"],
            user_id=d["user_id"],
            amount_cents=int(d["amount_cents"]),
            outcome=RefundOutcome(d["outcome"]),
            reason=d.get("reason", ""),
            timestamp=d.get("timestamp", ""),
            trace_id=d.get("trace_id", ""),
            attempt_index=int(d.get("attempt_index", 1)),
        )
