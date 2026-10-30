"""退款客服 Agent（确定性规则引擎实现）。

刻意不接大模型：评测要的是"同一份代码、同一个输入，永远得到同一条 trace"。
等大模型介入后，同一份用例集可以直接对比规则内核与模型内核的差异 ——
这正是 agent_audit.llm 里留的口子。

Agent 自己很朴素，真正的价值在它吐出的 rule_decisions 与 tool_calls：
那是审计与评测的唯一输入。
"""

from __future__ import annotations

from typing import Any, Optional

from ..domain.models import OrderStatus
from ..tools.base import ToolContext, ToolResult, ToolStatus
from .protocol import Action, Agent, AgentReply, RuleDecision
from .rules import RuleConfig, extract_order_id, rule


class RefundAgent(Agent):
    """规则驱动的退款客服 Agent。"""

    def __init__(
        self,
        config: RuleConfig | None = None,
        agent_id: str = "refund-agent-v1",
        display_name: str = "退款客服 Agent v1（基线）",
        description: str = "完整实现 R1-R4 四条业务规则的基线版本",
    ):
        self.config = config or RuleConfig()
        self.agent_id = agent_id
        self.display_name = display_name
        self.description = description
        self._user_id: str = ""
        self._pending: dict[str, str] = {}   # order_id -> 挂起原因
        self._asked_count = 0

    # ---------- 生命周期 ----------

    def reset(self, ctx: ToolContext, user_id: str) -> None:
        self._user_id = user_id
        self._pending.clear()
        self._asked_count = 0

    # ---------- 主流程 ----------

    def handle(self, message: str, ctx: ToolContext, turn: int = 1) -> AgentReply:
        decisions: list[RuleDecision] = []
        calls: list[ToolResult] = []

        def decide(rid: str, matched: bool, code: str, msg: str) -> RuleDecision:
            d = RuleDecision(rid, rule(rid).name, matched, code, msg, turn)
            decisions.append(d)
            return d

        known = [o.order_id for o in ctx.store.all_orders()]
        order_id, how = extract_order_id(message, known)

        # ---------- R1：无订单号先追问 ----------
        if order_id is None and self.config.guess_latest_order:
            order_id = self._guess_latest(ctx, calls)
            if order_id:
                how = "guessed_latest"

        if order_id is None:
            if self.config.ask_for_order_id:
                self._asked_count += 1
                decide("R1", True, "ORDER_ID_MISSING",
                       "用户消息中未识别到唯一订单号，需先追问")
                # 追问也要有用：把用户名下的订单列出来，帮用户定位
                lst = ctx.tools["list_user_orders"].run(ctx, user_id=self._user_id) \
                    if hasattr(ctx, "tools") else None
                if lst is not None:
                    calls.append(lst)
                return AgentReply(
                    text=self._ask_text(lst),
                    action=Action.ASK_ORDER_ID,
                    tool_calls=calls, rule_decisions=decisions,
                    meta={"extracted_order_id": None, "extract_how": how})
            # 规则被关掉：既没单号也不追问，只能含糊应答
            decide("R1", False, "ORDER_ID_RULE_DISABLED",
                   "追问规则已关闭，Agent 在缺少订单号的情况下继续处理")
            return AgentReply(
                text="好的，我先帮您看看怎么处理。",
                action=Action.INFO, tool_calls=calls, rule_decisions=decisions,
                meta={"extracted_order_id": None, "extract_how": how})

        decide("R1", False, "ORDER_ID_OK", f"已识别订单号 {order_id}（{how}）")

        # ---------- R4：超时挂起中的订单不得重试 ----------
        if order_id in self._pending:
            if self.config.no_retry_after_timeout and not self.config.retry_on_timeout:
                decide("R4", True, "REFUND_PENDING_NO_RETRY",
                       f"订单 {order_id} 处于超时挂起状态，拒绝再次发起退款")
                return AgentReply(
                    text=(f"订单 {order_id} 的退款正在处理中，支付网关尚未返回结果。"
                          f"我们会在 1 小时内完成对账并通知您，请不要重复提交退款申请。"),
                    action=Action.REFUND_PENDING, tool_calls=calls,
                    rule_decisions=decisions,
                    meta={"extracted_order_id": order_id, "extract_how": how,
                          "pending": True})
            decide("R4", False, "RETRY_RULE_DISABLED",
                   "超时禁重试规则被关闭，Agent 将对挂起订单再次发起退款")

        # ---------- 查询订单 ----------
        order = None
        if not self.config.skip_lookup:
            look = ctx.tools["lookup_order"].run(ctx, order_id=order_id)
            calls.append(look)
            if look.timed_out:
                return AgentReply(
                    text="订单查询服务暂时没有响应，请稍后重试或提供订单号让我再查一次。",
                    action=Action.REFUND_FAILED, tool_calls=calls,
                    rule_decisions=decisions,
                    meta={"extracted_order_id": order_id, "extract_how": how})
            if look.status is ToolStatus.ERROR or not look.data.get("found", False):
                return AgentReply(
                    text=f"没有查到订单 {order_id}，请核对订单号是否正确。",
                    action=Action.REFUND_FAILED, tool_calls=calls,
                    rule_decisions=decisions,
                    meta={"extracted_order_id": order_id, "extract_how": how})
            order = ctx.store.get(order_id)
        else:
            order = ctx.store.get(order_id)

        if order is None:
            return AgentReply(
                text=f"没有查到订单 {order_id}，请核对订单号是否正确。",
                action=Action.REFUND_FAILED, tool_calls=calls,
                rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how})

        # ---------- R2：跨用户订单拒绝 ----------
        if self.config.check_ownership and order.user_id != self._user_id:
            decide("R2", True, "CROSS_USER_REJECTED",
                   f"订单 {order_id} 属于用户 {order.user_id}，"
                   f"与当前会话用户 {self._user_id} 不一致")
            return AgentReply(
                text=(f"经核实，订单 {order_id} 并非您名下的订单，"
                      f"我无法为您办理这笔退款。如果您确认订单号有误，可以提供本人订单号后重试。"),
                action=Action.REFUSE, tool_calls=calls, rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how,
                      "owner_mismatch": True})
        if order.user_id != self._user_id:
            decide("R2", False, "OWNERSHIP_CHECK_SKIPPED",
                   "归属校验被关闭，Agent 未发现这是他人的订单")

        # ---------- R3：同一订单只能退款一次 ----------
        already = ctx.ledger.has_successful_refund(order_id) or \
            order.status is OrderStatus.REFUNDED
        if already and self.config.check_duplicate:
            decide("R3", True, "DUPLICATE_REFUND_REJECTED",
                   f"订单 {order_id} 已完成退款，拒绝重复打款")
            return AgentReply(
                text=(f"订单 {order_id} 此前已经完成退款，"
                      f"同一笔订单不能重复退款。如有其他问题，我可以帮您进一步核实。"),
                action=Action.REFUSE, tool_calls=calls, rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how,
                      "duplicate": True})
        if already:
            decide("R3", False, "DUPLICATE_CHECK_SKIPPED",
                   "幂等校验被关闭，Agent 对已退款订单继续发起退款")
        if order.status is OrderStatus.CLOSED:
            decide("R3", True, "ORDER_CLOSED_REJECTED",
                   f"订单 {order_id} 已关闭，不可退款")
            return AgentReply(
                text=(f"订单 {order_id} 已关闭，不在可退款范围内，"
                      f"这笔退款无法办理。如需帮助，我可以帮您转接人工客服。"),
                action=Action.REFUSE, tool_calls=calls, rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how,
                      "closed": True})

        # ---------- 执行退款 ----------
        res = ctx.tools["refund_order"].run(
            ctx, order_id=order_id, user_id=self._user_id, reason="用户申请退款")
        calls.append(res)

        if res.timed_out:
            self._pending[order_id] = "TIMEOUT"
            decide("R4", True, "REFUND_TIMEOUT_PENDING",
                   f"退款调用超时，订单 {order_id} 结果未知，转入挂起等待对账")
            if self.config.retry_on_timeout:
                # 缺陷行为：超时后立刻再打一次
                retry = ctx.tools["refund_order"].run(
                    ctx, order_id=order_id, user_id=self._user_id,
                    reason="超时重试")
                calls.append(retry)
                decide("R4", False, "TIMEOUT_RETRY_EXECUTED",
                       "超时后自动重试退款（违反 R4）")
                return self._after_retry(order_id, res, retry, calls, decisions, how)

            return AgentReply(
                text=(f"订单 {order_id} 的退款请求已提交，但支付网关响应超时，"
                      f"结果尚未确认。我会保持挂起并在对账后主动通知您，"
                      f"请不要重复提交，避免重复打款。"),
                action=Action.REFUND_PENDING, tool_calls=calls,
                rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how,
                      "timeout": True, "pending": True})

        if res.status is ToolStatus.OK and res.moved_money:
            decide("R3", False, "REFUND_EXECUTED", f"订单 {order_id} 退款成功")
            return AgentReply(
                text=(f"已为您办理订单 {order_id}（{order.item}）的退款，"
                      f"金额 {order.amount_yuan:.2f} 元，预计 1-3 个工作日到账。"),
                action=Action.REFUND_SUCCEEDED, tool_calls=calls,
                rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how})

        if res.status is ToolStatus.DENIED:
            return AgentReply(
                text=f"这笔退款无法办理：{res.error}",
                action=Action.REFUSE, tool_calls=calls, rule_decisions=decisions,
                meta={"extracted_order_id": order_id, "extract_how": how,
                      "denied": res.data.get("outcome", "")})

        return AgentReply(
            text=f"退款处理失败：{res.error or '支付网关异常'}。请稍后重试。",
            action=Action.REFUND_FAILED, tool_calls=calls, rule_decisions=decisions,
            meta={"extracted_order_id": order_id, "extract_how": how})

    # ---------- 辅助 ----------

    def _after_retry(self, order_id: str, first: ToolResult, retry: ToolResult,
                     calls: list[ToolResult], decisions: list[RuleDecision],
                     how: str) -> AgentReply:
        if retry.status is ToolStatus.OK and retry.moved_money:
            text = (f"订单 {order_id} 的退款已提交成功，金额将于 1-3 个工作日退回。")
            action = Action.REFUND_SUCCEEDED
        elif retry.timed_out:
            self._pending[order_id] = "TIMEOUT"
            text = (f"订单 {order_id} 的退款请求处理超时，请稍后查询结果。")
            action = Action.REFUND_PENDING
        else:
            text = f"订单 {order_id} 退款处理失败：{retry.error or '支付网关异常'}。"
            action = Action.REFUND_FAILED
        return AgentReply(text=text, action=action, tool_calls=calls,
                          rule_decisions=decisions,
                          meta={"extracted_order_id": order_id,
                                "extract_how": how, "retried": True})

    def _guess_latest(self, ctx: ToolContext, calls: list[ToolResult]) -> Optional[str]:
        """缺陷变体用：没有订单号时，擅自拿用户最近一笔可退订单。"""
        lst = ctx.tools["list_user_orders"].run(ctx, user_id=self._user_id)
        calls.append(lst)
        items = lst.data.get("orders", [])
        paid = [o for o in items if o.get("status") == OrderStatus.PAID.value]
        return paid[0]["order_id"] if paid else None

    def _ask_text(self, lst: Optional[ToolResult]) -> str:
        self._asked_count = max(self._asked_count, 1)
        base = "为了帮您办理退款，请提供需要退款的订单号（形如 SO-20261001-0001）。"
        if lst is not None and lst.ok and lst.data.get("orders"):
            names = "、".join(
                f"{o['order_id']}（{o['item']}）" for o in lst.data["orders"][:4])
            base += f"\n您名下的订单有：{names}。请告诉我其中要退的那一笔。"
        return base

    def describe(self) -> dict[str, Any]:
        d = super().describe()
        d["config"] = self.config.to_dict()
        d["disabled_rules"] = self.config.disabled_rules()
        return d
