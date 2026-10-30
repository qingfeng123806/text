"""Agent 变异体：故意带缺陷的版本。

这些变体不是玩具。它们是回归评测的"抗原" —— 一个从不失败的评测套件，
和没有评测套件是一回事。每个变体对应一次真实世界里常见的改动：

  v2  为了提升成功率，给退款加了超时重试 —— 典型的"好心办坏事"型回归
  v3  重构时漏拷了归属校验 —— 典型的人工失误型回归
  v4  为了让体验"更智能"，没单号时猜最近一单 —— 典型的体验优化引入越权
  v5  幂等预检被当作冗余逻辑删掉 —— 典型的"这判断多余"型回归

跑 `python run.py eval --agent all` 就能看到每条回归被精确指认到具体规则。
"""

from __future__ import annotations

from typing import Any

from .protocol import Agent
from .refund_agent import RefundAgent
from .rules import RuleConfig


def baseline_agent() -> RefundAgent:
    return RefundAgent(
        config=RuleConfig(),
        agent_id="refund-agent-v1",
        display_name="退款客服 Agent v1（基线）",
        description="完整实现 R1-R4 四条业务规则，作为回归基线",
    )


def timeout_retry_agent() -> RefundAgent:
    return RefundAgent(
        config=RuleConfig(
            retry_on_timeout=True,
            no_retry_after_timeout=False,
        ),
        agent_id="refund-agent-v2-timeout-retry",
        display_name="退款客服 Agent v2（超时重试回归）",
        description="R4 退化：退款超时后自动重试，可能造成重复打款",
    )


def no_ownership_agent() -> RefundAgent:
    return RefundAgent(
        config=RuleConfig(check_ownership=False),
        agent_id="refund-agent-v3-no-ownership",
        display_name="退款客服 Agent v3（缺归属校验）",
        description="R2 缺失：不对订单归属做校验，会向他人订单发起退款",
    )


def guess_order_agent() -> RefundAgent:
    return RefundAgent(
        config=RuleConfig(ask_for_order_id=False, guess_latest_order=True),
        agent_id="refund-agent-v4-guess-order",
        display_name="退款客服 Agent v4（擅自猜单）",
        description="R1 缺失：用户未提供订单号时擅自选择最近一笔订单退款",
    )


def no_idempotency_agent() -> RefundAgent:
    return RefundAgent(
        config=RuleConfig(check_duplicate=False),
        agent_id="refund-agent-v5-no-idempotency",
        display_name="退款客服 Agent v5（缺幂等预检）",
        description="R3 缺失：不预检重复退款，直接对已退订单发起退款调用",
    )


def reckless_agent() -> RefundAgent:
    """多条规则同时退化，用于验证评测在多重故障下的定位能力。"""
    return RefundAgent(
        config=RuleConfig(
            check_ownership=False,
            check_duplicate=False,
            ask_for_order_id=False,
            guess_latest_order=True,
            retry_on_timeout=True,
            no_retry_after_timeout=False,
        ),
        agent_id="refund-agent-v6-reckless",
        display_name="退款客服 Agent v6（多重退化）",
        description="R1-R4 全部退化，用于压力测试评测层的定位能力",
    )


_REGISTRY: dict[str, Any] = {
    "refund-agent-v1": baseline_agent,
    "refund-agent-v2-timeout-retry": timeout_retry_agent,
    "refund-agent-v3-no-ownership": no_ownership_agent,
    "refund-agent-v4-guess-order": guess_order_agent,
    "refund-agent-v5-no-idempotency": no_idempotency_agent,
    "refund-agent-v6-reckless": reckless_agent,
}


def available_agents() -> list[str]:
    return list(_REGISTRY.keys())


def get_agent(agent_id: str) -> Agent:
    if agent_id not in _REGISTRY:
        raise KeyError(
            f"未知 Agent: {agent_id!r}。可选: {', '.join(available_agents())}")
    return _REGISTRY[agent_id]()


def build_all_agents() -> list[Agent]:
    return [f() for f in _REGISTRY.values()]


def describe_all() -> list[dict[str, Any]]:
    return [f().describe() for f in _REGISTRY.values()]
