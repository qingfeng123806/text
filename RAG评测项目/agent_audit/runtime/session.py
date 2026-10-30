"""会话编排：把 Agent、工具、账本和审计日志串起来跑完一整个用例。

Session 是唯一允许"调用 Agent"的地方。这样做的好处是：
任何一次 Agent 行为，都必然经过审计，不存在绕过记录的旁路。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..agent.protocol import Agent
from ..domain.ledger import Ledger
from ..domain.order_store import OrderStore
from ..tools.base import ToolContext
from ..tools.faults import FaultInjector
from ..tools.refund_tools import build_default_tools
from .clock import Clock, make_clock
from .recorder import AuditLog
from .trace import Trace, Turn


class Session:
    def __init__(
        self,
        agent: Agent,
        store: OrderStore,
        ledger: Ledger,
        trace_id: str,
        run_id: str = "",
        case_id: str = "",
        case_title: str = "",
        user_id: str = "",
        injector: FaultInjector | None = None,
        audit: AuditLog | None = None,
        clock: Clock | None = None,
        deadline_ms: int = 1000,
        tools: Optional[dict[str, Any]] = None,
        tags: Optional[list[str]] = None,
    ):
        self.agent = agent
        self.store = store
        self.ledger = ledger
        self.trace_id = trace_id
        self.run_id = run_id
        self.case_id = case_id
        self.case_title = case_title
        self.user_id = user_id
        self.clock = clock or make_clock("virtual")
        self.injector = injector or FaultInjector()
        self.audit = audit
        self.ctx = ToolContext(
            store=store, ledger=ledger, clock=self.clock,
            injector=self.injector, trace_id=trace_id,
            deadline_ms=deadline_ms,
            tools=tools or build_default_tools(),
        )
        self.trace = Trace(
            trace_id=trace_id, run_id=run_id, case_id=case_id,
            case_title=case_title, agent_id=agent.agent_id, user_id=user_id,
            fault_specs=self.injector.to_dict(), tags=list(tags or []),
            created_at=datetime.now().isoformat(timespec="seconds"),
        )

    # ---------- 执行 ----------

    def run(self, messages: list[str]) -> Trace:
        if self.audit is not None:
            self.audit.append("trace_start", {
                "trace_id": self.trace_id,
                "run_id": self.run_id,
                "case_id": self.case_id,
                "agent_id": self.agent.agent_id,
                "user_id": self.user_id,
                "faults": self.injector.to_dict(),
                "deadline_ms": self.ctx.deadline_ms,
            }, trace_id=self.trace_id)

        self.trace.orders_before = self.store.snapshot()
        self.agent.reset(self.ctx, self.user_id)

        for i, msg in enumerate(messages, start=1):
            self._send(i, msg)

        self.trace.orders_after = self.store.snapshot()
        self.trace.ledger_snapshot = self.ledger.snapshot()

        if self.audit is not None:
            self.audit.append("trace_end", {
                "trace_id": self.trace_id,
                "turns": len(self.trace.turns),
                "money_moves": len(self.trace.money_moves()),
                "timeouts": len(self.trace.timeouts()),
                "ledger": self.ledger.snapshot(),
                "duplicate_payouts": self.ledger.orders_with_multiple_payouts(),
            }, trace_id=self.trace_id)

        return self.trace

    def _send(self, index: int, message: str) -> Turn:
        started = self.clock.now_ms()
        turn = Turn(index=index, user_message=message, started_ms=started)

        if self.audit is not None:
            self.audit.append("user_message", {
                "turn": index, "text": message,
            }, trace_id=self.trace_id)

        reply = self.agent.handle(message, self.ctx, turn=index)
        turn.reply = reply
        turn.ended_ms = self.clock.now_ms()
        self.trace.turns.append(turn)

        if self.audit is not None:
            self.audit.append("agent_reply", {
                "turn": index,
                "text": reply.text,
                "action": reply.action.value,
                "rule_decisions": [r.to_dict() for r in reply.rule_decisions],
                "tool_calls": [t.to_dict() for t in reply.tool_calls],
                "moved_money": reply.moved_money,
                "duration_ms": turn.ended_ms - turn.started_ms,
            }, trace_id=self.trace_id)

        return turn


def run_session(
    agent: Agent,
    store: OrderStore,
    messages: list[str],
    user_id: str,
    case_id: str = "",
    case_title: str = "",
    run_id: str = "",
    trace_id: str = "",
    faults: Optional[list[dict[str, Any]]] = None,
    audit: AuditLog | None = None,
    clock_mode: str = "virtual",
    deadline_ms: int = 1000,
    tags: Optional[list[str]] = None,
) -> tuple[Trace, OrderStore, Ledger]:
    """便捷入口：造一套干净的环境把会话跑完，返回 (trace, store, ledger)。"""
    from ..tools.faults import FaultInjector

    ledger = Ledger()
    clock = make_clock(clock_mode)
    injector = FaultInjector.from_list(faults)
    tid = trace_id or f"{run_id or 'run'}-{case_id}-{agent.agent_id}"

    sess = Session(
        agent=agent, store=store, ledger=ledger, trace_id=tid,
        run_id=run_id, case_id=case_id, case_title=case_title,
        user_id=user_id, injector=injector, audit=audit, clock=clock,
        deadline_ms=deadline_ms, tags=tags,
    )
    trace = sess.run(messages)
    return trace, store, ledger
