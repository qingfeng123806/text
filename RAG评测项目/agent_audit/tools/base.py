"""工具执行基座。

每个工具的调用都会产出一条 ToolResult，它是审计 trace 的基本单元：
谁调用的、传了什么参、花了多久、成功还是超时、账本认不认。
Agent 事后怎么解释都不重要，ToolResult 是当时的事实。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from ..domain.ledger import Ledger
from ..domain.order_store import OrderStore
from ..runtime.clock import Clock
from .faults import FaultInjector, FaultKind, FaultSpec


class ToolStatus(str, Enum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"
    ERROR = "ERROR"
    DENIED = "DENIED"      # 业务层拒绝（不属于该用户 / 已退款），工具没动钱


@dataclass
class ToolResult:
    tool: str
    args: dict[str, Any]
    status: ToolStatus
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 0
    call_index: int = 1
    fault: Optional[str] = None
    ledger_seq: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status is ToolStatus.OK

    @property
    def timed_out(self) -> bool:
        return self.status is ToolStatus.TIMEOUT

    @property
    def moved_money(self) -> bool:
        """本次调用是否真的动了钱（审计最关心的字段）。"""
        return bool(self.data.get("money_moved", False))

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "args": self.args,
            "status": self.status.value,
            "data": self.data,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "call_index": self.call_index,
            "fault": self.fault,
            "ledger_seq": self.ledger_seq,
            "money_moved": self.moved_money,
        }


class ToolContext:
    """一次会话共享的工具执行上下文。

    tools 由运行时注入（避免工具层与工具实现互相 import 形成环）。
    """

    def __init__(
        self,
        store: OrderStore,
        ledger: Ledger,
        clock: Clock,
        injector: FaultInjector | None = None,
        trace_id: str = "",
        deadline_ms: int = 1000,
        tools: Optional[dict[str, "Tool"]] = None,
    ):
        self.store = store
        self.ledger = ledger
        self.clock = clock
        self.injector = injector or FaultInjector()
        self.trace_id = trace_id
        self.deadline_ms = deadline_ms
        self.tools: dict[str, Tool] = tools or {}

    def reset_call_counters(self) -> None:
        self.injector.reset()

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        if name not in self.tools:
            raise ToolError(f"未注册的工具: {name}")
        return self.tools[name].run(self, **kwargs)


class ToolError(Exception):
    pass


class ToolTimeout(Exception):
    pass


class Tool:
    name: str = "tool"
    description: str = ""

    def run(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        raise NotImplementedError

    # ---------- 子类可用的钩子 ----------

    def _begin(self, ctx: ToolContext, args: dict[str, Any]) -> tuple[int, int, Optional[FaultSpec]]:
        """返回 (起始时间, 调用序号, 命中的故障)。"""
        call_index = ctx.injector.next_call_index(self.name)
        fault = ctx.injector.match(self.name, call_index, args.get("order_id"))
        return ctx.clock.now_ms(), call_index, fault

    def _elapsed(self, ctx: ToolContext, start: int) -> int:
        return ctx.clock.now_ms() - start

    def _simulate_latency(self, ctx: ToolContext, fault: Optional[FaultSpec],
                          base_ms: int = 40) -> None:
        """消耗时间：正常调用有基础耗时，故障调用会耗尽甚至超出 deadline。"""
        ctx.clock.sleep_ms(base_ms)
        if fault is None:
            return
        if fault.kind is FaultKind.TIMEOUT:
            # 睡到超过 deadline，让超时判定自然发生，而不是硬编码一个结果
            over = ctx.deadline_ms - base_ms + fault.extra_ms
            if over > 0:
                ctx.clock.sleep_ms(over)
        elif fault.kind is FaultKind.SLOW:
            ctx.clock.sleep_ms(max(0, ctx.deadline_ms - base_ms - 20))
        elif fault.kind is FaultKind.ERROR:
            ctx.clock.sleep_ms(20)
        elif fault.kind is FaultKind.FLAKY:
            ctx.clock.sleep_ms(15)
