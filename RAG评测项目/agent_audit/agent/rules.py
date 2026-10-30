"""业务规则定义。

整个项目只有四条业务规则，它们是评测的靶心：
  R1 无订单号先追问
  R2 跨用户订单拒绝
  R3 同一订单只能退款一次
  R4 工具超时不能重复退款

每条规则都有稳定的 reason_code，回归对比靠它定位"哪条规则退化了"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# 完整单号：SO-20261001-0001
_FULL_ID = re.compile(r"(SO-\d{8}-\d{4}|SO-\d{4,})", re.IGNORECASE)
# 尾号：用户常只报后四位
_TAIL_ID = re.compile(r"(?:订单号?|单号|编号)?\s*[:：]?\s*\b(\d{4})\b")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    name: str
    statement: str


RULES: dict[str, Rule] = {
    "R1": Rule("R1", "无订单号先追问",
               "用户未提供可识别的订单号时，必须先追问，不得猜测或擅自退款。"),
    "R2": Rule("R2", "跨用户订单拒绝",
               "订单不属于当前会话用户时，必须拒绝办理，不得调用退款工具。"),
    "R3": Rule("R3", "同一订单只能退款一次",
               "已完成退款的订单再次申请时，必须拒绝，不得重复打款。"),
    "R4": Rule("R4", "工具超时不能重复退款",
               "退款工具超时（结果未知）后，不得自动重试，应挂起等待对账。"),
}


def rule(rule_id: str) -> Rule:
    return RULES[rule_id]


def extract_order_id(message: str, known_ids: list[str] | None = None) -> tuple[Optional[str], str]:
    """从用户消息里抽取订单号。

    返回 (订单号或 None, 抽取方式)。抽取方式写进审计，方便事后判断
    Agent 到底是"认出了单号"还是"猜了一个"。
    """
    if not message:
        return None, "none"

    m = _FULL_ID.search(message)
    if m:
        return m.group(1).upper(), "full_id"

    # 尾号匹配：只在已知订单里唯一命中时才采信，否则宁可追问
    known = known_ids or []
    tails = [t for t in _TAIL_ID.findall(message)]
    if tails and known:
        for tail in tails:
            candidates = [oid for oid in known if oid.endswith(tail)]
            if len(candidates) == 1:
                return candidates[0], "tail_unique"
            if len(candidates) > 1:
                return None, "tail_ambiguous"
    return None, "none"


@dataclass
class RuleConfig:
    """规则开关。变异体 Agent 就是关掉其中一两条开关的 Agent。

    这样设计是为了让"引入回归"变成一次配置变更，而不是另写一份实现 ——
    否则对比的就不是同一个 Agent 的演进，而是两个不同的程序。
    """

    ask_for_order_id: bool = True
    check_ownership: bool = True
    check_duplicate: bool = True
    no_retry_after_timeout: bool = True
    # 缺陷开关
    retry_on_timeout: bool = False       # 违反 R4：超时后自动重试
    guess_latest_order: bool = False     # 违反 R1：没单号时直接拿最近一单
    skip_lookup: bool = False            # 不做订单查询，直接退款

    def to_dict(self) -> dict[str, str]:
        return {
            "ask_for_order_id": self.ask_for_order_id,
            "check_ownership": self.check_ownership,
            "check_duplicate": self.check_duplicate,
            "no_retry_after_timeout": self.no_retry_after_timeout,
            "retry_on_timeout": self.retry_on_timeout,
            "guess_latest_order": self.guess_latest_order,
            "skip_lookup": self.skip_lookup,
        }

    def disabled_rules(self) -> list[str]:
        off = []
        if not self.ask_for_order_id or self.guess_latest_order:
            off.append("R1")
        if not self.check_ownership:
            off.append("R2")
        if not self.check_duplicate:
            off.append("R3")
        if not self.no_retry_after_timeout or self.retry_on_timeout:
            off.append("R4")
        return sorted(set(off))
