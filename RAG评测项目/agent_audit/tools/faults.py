"""故障注入声明。

规则 4（工具超时不能重复退款）如果只是嘴上说说，是没法被评测的 ——
必须能确定性地把工具打超时。这里把故障描述成数据，写进用例里，
同一份用例每次运行都得到同一个故障，回归对比才有意义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class FaultKind(str, Enum):
    TIMEOUT = "timeout"       # 调用超时，结果未知
    ERROR = "error"           # 网关异常，明确失败
    SLOW = "slow"             # 慢但最终成功
    FLAKY = "flaky"           # 前 N 次失败，之后成功


@dataclass
class FaultSpec:
    """一次故障注入。

    tool:      目标工具名
    kind:      故障类型
    on_call:   在第几次调用该工具时触发（1 基，None 表示每次）
    order_id:  限定某个订单触发（None 表示不限）
    extra_ms:  超时时超出 deadline 的毫秒数
    message:   错误描述
    settle_on_timeout:
               超时时后台其实已经把钱退了。这是支付领域最凶险的一种情况：
               调用方看到超时（结果未知），但资金已经发生转移。
               此时 Agent 若盲目重试，就会造成重复打款。
    """

    tool: str
    kind: FaultKind = FaultKind.TIMEOUT
    on_call: Optional[int] = None
    order_id: Optional[str] = None
    extra_ms: int = 80
    message: str = "支付网关无响应"
    settle_on_timeout: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "kind": self.kind.value,
            "on_call": self.on_call,
            "order_id": self.order_id,
            "extra_ms": self.extra_ms,
            "message": self.message,
            "settle_on_timeout": self.settle_on_timeout,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FaultSpec":
        return cls(
            tool=d["tool"],
            kind=FaultKind(d.get("kind", "timeout")),
            on_call=d.get("on_call"),
            order_id=d.get("order_id"),
            extra_ms=int(d.get("extra_ms", 80)),
            message=d.get("message", "支付网关无响应"),
            settle_on_timeout=bool(d.get("settle_on_timeout", False)),
        )


class FaultInjector:
    """按调用次数和订单匹配，决定是否对本次调用注入故障。"""

    def __init__(self, specs: list[FaultSpec] | None = None):
        self._specs = list(specs or [])
        self._call_counter: dict[str, int] = {}

    def next_call_index(self, tool: str) -> int:
        self._call_counter[tool] = self._call_counter.get(tool, 0) + 1
        return self._call_counter[tool]

    def reset(self) -> None:
        self._call_counter.clear()

    def match(self, tool: str, call_index: int, order_id: Optional[str]) -> Optional[FaultSpec]:
        for s in self._specs:
            if s.tool != tool:
                continue
            if s.on_call is not None and s.on_call != call_index:
                continue
            if s.order_id is not None and s.order_id != order_id:
                continue
            return s
        return None

    def to_dict(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._specs]

    @classmethod
    def from_list(cls, items: list[dict[str, Any]] | None) -> "FaultInjector":
        return cls([FaultSpec.from_dict(d) for d in (items or [])])
