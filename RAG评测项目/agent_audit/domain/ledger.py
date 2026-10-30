"""退款账本：资金动作的唯一裁决者。

设计要点 —— 幂等不在 Agent 侧，而在账本侧。
Agent 就算发了十次退款请求，账本也只允许第一次真正动钱，
其余一律 REJECTED_DUPLICATE。这样"同一订单只能退款一次"就变成了
一个可被审计验证的客观事实，而不是对 Agent 的信任。

账本同时记录超时/异常尝试。超时是"结果未知"，账本会保留这条痕迹，
让审计层能够判断 Agent 在超时后有没有再次发起退款。
"""

from __future__ import annotations

from typing import Any, Optional

from .models import Order, OrderStatus, RefundOutcome, RefundRecord


class Ledger:
    def __init__(self) -> None:
        self._records: list[RefundRecord] = []
        self._seq = 0

    # ---------- 写入 ----------

    def _append(self, order: Order, outcome: RefundOutcome, reason: str,
                trace_id: str) -> RefundRecord:
        self._seq += 1
        attempt_index = sum(
            1 for r in self._records
            if r.order_id == order.order_id and r.trace_id == trace_id
        ) + 1
        rec = RefundRecord(
            seq=self._seq,
            order_id=order.order_id,
            user_id=order.user_id,
            amount_cents=order.amount_cents,
            outcome=outcome,
            reason=reason,
            trace_id=trace_id,
            attempt_index=attempt_index,
        )
        self._records.append(rec)
        return rec

    # ---------- 查询（Agent 与评测共用）----------

    def has_successful_refund(self, order_id: str) -> bool:
        return any(r.order_id == order_id and r.is_money_moved for r in self._records)

    def attempts(self, order_id: str) -> list[RefundRecord]:
        return [r for r in self._records if r.order_id == order_id]

    def successful_refunds(self) -> list[RefundRecord]:
        return [r for r in self._records if r.is_money_moved]

    def all_records(self) -> list[RefundRecord]:
        return list(self._records)

    def orders_with_multiple_payouts(self) -> list[str]:
        """返回真正被重复打款的订单 —— 这是最严重的资金事故。"""
        counter: dict[str, int] = {}
        for r in self._records:
            if r.is_money_moved:
                counter[r.order_id] = counter.get(r.order_id, 0) + 1
        return sorted(oid for oid, n in counter.items() if n > 1)

    # ---------- 核心动作 ----------

    def try_refund(self, order: Order, requester_id: str, trace_id: str) -> RefundRecord:
        """尝试退款。返回落账的流水记录，绝不抛异常给 Agent。"""
        if requester_id != order.user_id:
            return self._append(
                order, RefundOutcome.REJECTED_NOT_OWNER,
                f"订单 {order.order_id} 属于用户 {order.user_id}，"
                f"请求方 {requester_id} 无权操作", trace_id)

        if self.has_successful_refund(order.order_id):
            return self._append(
                order, RefundOutcome.REJECTED_DUPLICATE,
                f"订单 {order.order_id} 已完成退款，拒绝重复打款", trace_id)

        if order.status is OrderStatus.REFUNDED:
            return self._append(
                order, RefundOutcome.REJECTED_DUPLICATE,
                f"订单 {order.order_id} 状态为 REFUNDED，拒绝重复打款", trace_id)

        if order.status is OrderStatus.CLOSED:
            return self._append(
                order, RefundOutcome.REJECTED_NOT_REFUNDABLE,
                f"订单 {order.order_id} 已关闭，不可退款", trace_id)

        # 真正的资金动作
        return self._append(order, RefundOutcome.SUCCESS, "退款成功，预计 1-3 个工作日到账", trace_id)

    def record_not_found(self, order_id: str, user_id: str, trace_id: str) -> RefundRecord:
        """订单不存在时的留痕。虽然没动钱，但 Agent 试图操作一张不存在的单子，
        这件事本身值得记一笔。"""
        self._seq += 1
        rec = RefundRecord(
            seq=self._seq, order_id=order_id, user_id=user_id, amount_cents=0,
            outcome=RefundOutcome.REJECTED_NOT_FOUND,
            reason=f"订单 {order_id} 不存在", trace_id=trace_id, attempt_index=1,
        )
        self._records.append(rec)
        return rec

    def record_timeout(self, order: Order, trace_id: str) -> RefundRecord:
        """记录一次超时尝试：钱可能没动，也可能动了，结果未知。"""
        return self._append(
            order, RefundOutcome.TIMEOUT,
            "支付网关调用超时，结果未知（不得自动重试）", trace_id)

    def record_error(self, order: Order, trace_id: str, reason: str = "") -> RefundRecord:
        return self._append(
            order, RefundOutcome.ERROR, reason or "支付网关异常", trace_id)

    # ---------- 快照 / 序列化 ----------

    def snapshot(self) -> dict[str, Any]:
        return {
            "total_records": len(self._records),
            "successful": [r.order_id for r in self.successful_refunds()],
            "duplicate_payouts": self.orders_with_multiple_payouts(),
            "records": [r.to_dict() for r in self._records],
        }

    def to_dict(self) -> dict[str, Any]:
        return self.snapshot()
