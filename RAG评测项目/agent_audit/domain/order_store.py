"""订单仓储：只读查询 + 状态流转。

跨用户拒绝这条规则的第一道防线在这里 —— 订单归属是客观事实，
不依赖 Agent 自觉。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .models import Order, OrderStatus


class OrderStore:
    def __init__(self, orders: list[Order] | None = None):
        self._orders: dict[str, Order] = {}
        for o in orders or []:
            self._orders[o.order_id] = o

    # ---------- 构造 ----------

    @classmethod
    def from_file(cls, path: str | Path) -> "OrderStore":
        p = Path(path)
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls([Order.from_dict(d) for d in raw.get("orders", [])])

    def clone(self) -> "OrderStore":
        import copy
        return OrderStore([copy.deepcopy(o) for o in self._orders.values()])

    # ---------- 查询 ----------

    def get(self, order_id: str) -> Optional[Order]:
        return self._orders.get(order_id)

    def exists(self, order_id: str) -> bool:
        return order_id in self._orders

    def belongs_to(self, order_id: str, user_id: str) -> bool:
        o = self._orders.get(order_id)
        return o is not None and o.user_id == user_id

    def list_user_orders(self, user_id: str) -> list[Order]:
        return [o for o in self._orders.values() if o.user_id == user_id]

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())

    # ---------- 状态流转 ----------

    def mark(self, order_id: str, status: OrderStatus) -> None:
        o = self._orders.get(order_id)
        if o is not None:
            o.status = status

    def to_dict(self) -> dict[str, Any]:
        return {"orders": [o.to_dict() for o in self._orders.values()]}

    def snapshot(self) -> dict[str, Any]:
        """用于评测前后比对的状态快照。"""
        return {
            o.order_id: {"user_id": o.user_id, "status": o.status.value,
                         "amount_cents": o.amount_cents}
            for o in self._orders.values()
        }
