"""退款客服 Agent 可用的工具集。

刻意保持工具"笨"：它们只做事实查询和资金动作，不做业务判断。
订单归属、能否退款这类判断留给 Agent 的规则层 —— 这样当 Agent 判断错时，
审计才能区分"Agent 决策错误"和"工具执行错误"。

唯一的例外是账本：它也做重复退款拦截。这是纵深防御，
Agent 漏判了，账本兜住；账本没兜住的（比如超时后重试），才由评测的不变式抓出来。
"""

from __future__ import annotations

from typing import Any, Optional

from ..domain.models import Order, OrderStatus, RefundOutcome
from ..runtime.clock import Clock
from ..domain.ledger import Ledger
from ..domain.order_store import OrderStore
from .base import Tool, ToolContext, ToolResult, ToolStatus
from .faults import FaultKind, FaultSpec

# 各类操作的基础耗时（毫秒），让 trace 的时间线看起来可信
_LATENCY = {
    "lookup_order": 60,
    "list_user_orders": 90,
    "refund_order": 220,
    "notify_user": 30,
}


class LookupOrderTool(Tool):
    name = "lookup_order"
    description = "按订单号查询订单详情与归属用户"

    def run(self, ctx: ToolContext, order_id: str = "", **_: Any) -> ToolResult:
        args = {"order_id": order_id}
        start, call_index, fault = self._begin(ctx, args)
        self._simulate_latency(ctx, fault, _LATENCY[self.name])
        elapsed = self._elapsed(ctx, start)

        if fault is not None and fault.kind is FaultKind.ERROR:
            return ToolResult(self.name, args, ToolStatus.ERROR,
                              error=fault.message, duration_ms=elapsed,
                              call_index=call_index, fault="error")
        if elapsed > ctx.deadline_ms:
            return ToolResult(self.name, args, ToolStatus.TIMEOUT,
                              error="订单服务查询超时", duration_ms=elapsed,
                              call_index=call_index, fault="timeout")

        order = ctx.store.get(order_id)
        if order is None:
            return ToolResult(self.name, args, ToolStatus.DENIED,
                              data={"found": False}, error=f"订单 {order_id} 不存在",
                              duration_ms=elapsed, call_index=call_index)

        return ToolResult(self.name, args, ToolStatus.OK, duration_ms=elapsed,
                          call_index=call_index, data={
                              "found": True,
                              "order_id": order.order_id,
                              "user_id": order.user_id,
                              "item": order.item,
                              "amount_cents": order.amount_cents,
                              "amount_yuan": order.amount_yuan,
                              "status": order.status.value,
                          })


class ListUserOrdersTool(Tool):
    name = "list_user_orders"
    description = "列出某用户名下的全部订单"

    def run(self, ctx: ToolContext, user_id: str = "", **_: Any) -> ToolResult:
        args = {"user_id": user_id}
        start, call_index, fault = self._begin(ctx, args)
        self._simulate_latency(ctx, fault, _LATENCY[self.name])
        elapsed = self._elapsed(ctx, start)

        if elapsed > ctx.deadline_ms:
            return ToolResult(self.name, args, ToolStatus.TIMEOUT,
                              error="订单服务查询超时", duration_ms=elapsed,
                              call_index=call_index, fault="timeout")

        orders = ctx.store.list_user_orders(user_id)
        return ToolResult(self.name, args, ToolStatus.OK, duration_ms=elapsed,
                          call_index=call_index, data={
                              "user_id": user_id,
                              "count": len(orders),
                              "orders": [{
                                  "order_id": o.order_id, "item": o.item,
                                  "amount_cents": o.amount_cents,
                                  "status": o.status.value,
                              } for o in orders],
                          })


class RefundOrderTool(Tool):
    """执行退款。这是唯一会动钱的工具，也是全部风险所在。"""

    name = "refund_order"
    description = "对指定订单执行退款（资金动作，不可重复）"

    def run(self, ctx: ToolContext, order_id: str = "", user_id: str = "",
            reason: str = "", **_: Any) -> ToolResult:
        args = {"order_id": order_id, "user_id": user_id, "reason": reason}
        start, call_index, fault = self._begin(ctx, args)
        self._simulate_latency(ctx, fault, _LATENCY[self.name])
        elapsed = self._elapsed(ctx, start)

        order = ctx.store.get(order_id)

        # ---- 超时分支：结果未知，是最需要谨慎处理的路径 ----
        if elapsed > ctx.deadline_ms or (fault is not None and fault.kind is FaultKind.TIMEOUT):
            return self._timeout_result(ctx, args, order, user_id, elapsed, call_index, fault)

        if fault is not None and fault.kind is FaultKind.ERROR:
            if order is not None:
                rec = ctx.ledger.record_error(order, ctx.trace_id, fault.message)
                return ToolResult(self.name, args, ToolStatus.ERROR,
                                  data={"money_moved": False}, error=fault.message,
                                  duration_ms=elapsed, call_index=call_index,
                                  fault="error", ledger_seq=rec.seq)
            return ToolResult(self.name, args, ToolStatus.ERROR,
                              data={"money_moved": False}, error=fault.message,
                              duration_ms=elapsed, call_index=call_index, fault="error")

        if fault is not None and fault.kind is FaultKind.FLAKY and call_index <= 1:
            if order is not None:
                rec = ctx.ledger.record_error(order, ctx.trace_id, "支付网关抖动，调用失败")
            return ToolResult(self.name, args, ToolStatus.ERROR,
                              data={"money_moved": False}, error="支付网关抖动，调用失败",
                              duration_ms=elapsed, call_index=call_index, fault="flaky",
                              ledger_seq=(rec.seq if order is not None else None))

        if order is None:
            rec = ctx.ledger.record_not_found(order_id, user_id, ctx.trace_id)
            return ToolResult(self.name, args, ToolStatus.ERROR,
                              data={"money_moved": False}, error=f"订单 {order_id} 不存在",
                              duration_ms=elapsed, call_index=call_index,
                              ledger_seq=rec.seq)

        rec = self._commit(ctx, order, user_id)
        return self._from_record(ctx, args, order, rec, elapsed, call_index)

    # ---------- 内部 ----------

    def _commit(self, ctx: ToolContext, order: Order, user_id: str) -> Any:
        """落账并同步订单状态。

        这两件事必须绑在一起做。曾经把它们分散在超时分支和正常分支各写一遍，
        结果超时后台结算时账本记了 SUCCESS、订单状态却还是 PAID，账实不一致。
        现在成功路径和超时结算路径都只走这一个入口。
        """
        rec = ctx.ledger.try_refund(order, user_id, ctx.trace_id)
        if rec.is_money_moved:
            ctx.store.mark(order.order_id, OrderStatus.REFUNDED)
        return rec

    def _timeout_result(self, ctx: ToolContext, args: dict[str, Any],
                        order: Optional[Order], user_id: str, elapsed: int,
                        call_index: int, fault: Optional[FaultSpec]) -> ToolResult:
        settle = bool(fault and fault.settle_on_timeout)
        if settle and order is not None:
            # 凶险场景：钱其实已经退了，但调用方只看到超时
            rec = self._commit(ctx, order, user_id)
            return ToolResult(
                self.name, args, ToolStatus.TIMEOUT,
                data={"money_moved": rec.is_money_moved, "settled_in_background": True},
                error="支付网关响应超时，结果未知",
                duration_ms=elapsed, call_index=call_index,
                fault="timeout", ledger_seq=rec.seq)

        if order is not None:
            rec = ctx.ledger.record_timeout(order, ctx.trace_id)
            seq: Optional[int] = rec.seq
        else:
            seq = None
        return ToolResult(
            self.name, args, ToolStatus.TIMEOUT,
            data={"money_moved": False, "settled_in_background": False},
            error="支付网关响应超时，结果未知",
            duration_ms=elapsed, call_index=call_index, fault="timeout", ledger_seq=seq)

    def _from_record(self, ctx: ToolContext, args: dict[str, Any], order: Order,
                     rec: Any, elapsed: int, call_index: int) -> ToolResult:
        moved = rec.is_money_moved
        if moved:
            status = ToolStatus.OK
            error = ""
        elif rec.outcome is RefundOutcome.REJECTED_NOT_OWNER:
            status = ToolStatus.DENIED
            error = rec.reason
        elif rec.outcome in (RefundOutcome.REJECTED_DUPLICATE,
                             RefundOutcome.REJECTED_NOT_REFUNDABLE):
            status = ToolStatus.DENIED
            error = rec.reason
        else:
            status = ToolStatus.ERROR
            error = rec.reason

        return ToolResult(
            self.name, args, status,
            data={
                "money_moved": moved,
                "order_id": order.order_id,
                "amount_cents": order.amount_cents,
                "amount_yuan": order.amount_yuan,
                "outcome": rec.outcome.value,
            },
            error=error, duration_ms=elapsed, call_index=call_index, ledger_seq=rec.seq)


class NotifyUserTool(Tool):
    name = "notify_user"
    description = "向用户发送消息通知（不产生资金动作）"

    def run(self, ctx: ToolContext, user_id: str = "", message: str = "", **_: Any) -> ToolResult:
        args = {"user_id": user_id, "message": message}
        start, call_index, _ = self._begin(ctx, args)
        self._simulate_latency(ctx, None, _LATENCY[self.name])
        elapsed = self._elapsed(ctx, start)
        return ToolResult(self.name, args, ToolStatus.OK, duration_ms=elapsed,
                          call_index=call_index, data={"delivered": True})


def build_default_tools() -> dict[str, Tool]:
    tools = [LookupOrderTool(), ListUserOrdersTool(), RefundOrderTool(), NotifyUserTool()]
    return {t.name: t for t in tools}
