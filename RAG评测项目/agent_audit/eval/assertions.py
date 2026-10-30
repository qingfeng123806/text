"""断言引擎：把"这个场景应该怎样"翻译成对 trace 的机器检查。

每条断言都返回 expected / actual 两侧的值。
评测报告里最有说服力的不是"通过/失败"两个字，而是
"期望退款调用 0 次，实际 2 次，分别在第 1、2 轮"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..runtime.trace import Trace


@dataclass
class AssertionResult:
    kind: str
    passed: bool
    expected: Any
    actual: Any
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "passed": self.passed,
            "expected": self.expected, "actual": self.actual,
            "message": self.message, "detail": self.detail,
        }


def _ok(kind: str, expected: Any, actual: Any, msg: str,
        **detail: Any) -> AssertionResult:
    return AssertionResult(kind, True, expected, actual, msg, detail)


def _fail(kind: str, expected: Any, actual: Any, msg: str,
          **detail: Any) -> AssertionResult:
    return AssertionResult(kind, False, expected, actual, msg, detail)


# ============================================================
# 具体断言
# ============================================================

def check_action(trace: Trace, spec: Any) -> AssertionResult:
    """最终轮的动作类型。spec 可以是字符串或字符串列表。"""
    final = trace.final_reply()
    actual = final.action.value if final else "NONE"
    expected = spec if isinstance(spec, list) else [spec]
    if actual in expected:
        return _ok("action", expected, actual, f"最终动作 {actual} 符合预期")
    return _fail("action", expected, actual,
                 f"最终动作不符：期望 {'/'.join(expected)}，实际 {actual}",
                 final_text=(final.text[:120] if final else ""))


def check_turn_action(trace: Trace, spec: dict[str, Any]) -> AssertionResult:
    """指定轮次的动作：{"2": "REFUND_SUCCEEDED"}"""
    problems = []
    for key, want in spec.items():
        turn_idx = int(key)
        want_list = want if isinstance(want, list) else [want]
        actual = "NONE"
        for t in trace.turns:
            if t.index == turn_idx and t.reply is not None:
                actual = t.reply.action.value
                break
        if actual not in want_list:
            problems.append(f"第 {turn_idx} 轮期望 {'/'.join(want_list)}，实际 {actual}")
    if problems:
        return _fail("turn_action", spec, problems, "；".join(problems))
    return _ok("turn_action", spec, "all-match", "各轮次动作均符合预期")


def check_reply_contains_any(trace: Trace, spec: list[str]) -> AssertionResult:
    final = trace.final_reply()
    text = final.text if final else ""
    hit = [w for w in spec if w in text]
    if hit:
        return _ok("reply_contains_any", spec, hit, f"回复命中关键词 {hit}")
    return _fail("reply_contains_any", spec, [],
                 f"回复中未找到任何期望关键词 {spec}", reply=text[:200])


def check_reply_contains_all(trace: Trace, spec: list[str]) -> AssertionResult:
    final = trace.final_reply()
    text = final.text if final else ""
    missing = [w for w in spec if w not in text]
    if not missing:
        return _ok("reply_contains_all", spec, spec, "回复包含全部期望关键词")
    return _fail("reply_contains_all", spec, missing,
                 f"回复缺少关键词 {missing}", reply=text[:200])


def check_reply_not_contains(trace: Trace, spec: list[str]) -> AssertionResult:
    final = trace.final_reply()
    text = final.text if final else ""
    hit = [w for w in spec if w in text]
    if not hit:
        return _ok("reply_not_contains", spec, [], "回复未出现禁止话术")
    return _fail("reply_not_contains", spec, hit,
                 f"回复中出现了不应出现的措辞 {hit}", reply=text[:200])


def check_reply_regex(trace: Trace, spec: str) -> AssertionResult:
    final = trace.final_reply()
    text = final.text if final else ""
    if re.search(spec, text):
        return _ok("reply_regex", spec, spec, "回复匹配期望模式")
    return _fail("reply_regex", spec, None,
                 f"回复未匹配正则 {spec}", reply=text[:200])


def check_tool_count(trace: Trace, spec: dict[str, int]) -> AssertionResult:
    """某工具被调用的次数，精确匹配。"""
    problems, actual_map = [], {}
    for tool, want in spec.items():
        got = trace.count_tool(tool)
        actual_map[tool] = got
        if got != want:
            problems.append(f"{tool} 期望 {want} 次，实际 {got} 次")
    if problems:
        return _fail("tool_count", spec, actual_map, "；".join(problems))
    return _ok("tool_count", spec, actual_map, "工具调用次数符合预期")


def check_tool_count_max(trace: Trace, spec: dict[str, int]) -> AssertionResult:
    """某工具被调用次数的上限。"""
    problems, actual_map = [], {}
    for tool, limit in spec.items():
        got = trace.count_tool(tool)
        actual_map[tool] = got
        if got > limit:
            problems.append(f"{tool} 期望至多 {limit} 次，实际 {got} 次")
    if problems:
        return _fail("tool_count_max", spec, actual_map, "；".join(problems))
    return _ok("tool_count_max", spec, actual_map, "工具调用次数未超上限")


def check_money_move_count(trace: Trace, spec: int) -> AssertionResult:
    got = len(trace.money_moves())
    if got == spec:
        return _ok("money_move_count", spec, got, f"实际打款 {got} 次，符合预期")
    return _fail("money_move_count", spec, got,
                 f"资金动作次数不符：期望 {spec} 次，实际 {got} 次",
                 orders=[c.args.get("order_id") for c in trace.money_moves()])


def check_timeout_count(trace: Trace, spec: int) -> AssertionResult:
    got = len(trace.timeouts())
    if got == spec:
        return _ok("timeout_count", spec, got, f"超时 {got} 次，符合预期")
    return _fail("timeout_count", spec, got,
                 f"超时次数不符：期望 {spec} 次，实际 {got} 次")


def check_rule_matched(trace: Trace, spec: dict[str, bool]) -> AssertionResult:
    """某条规则是否被命中过。"""
    seen: dict[str, bool] = {}
    for r in trace.all_rule_decisions:
        seen[r.rule_id] = seen.get(r.rule_id, False) or bool(r.matched)
    problems = []
    for rid, want in spec.items():
        got = seen.get(rid, False)
        if got != want:
            problems.append(
                f"规则 {rid} 期望命中={want}，实际命中={got}")
    if problems:
        return _fail("rule_matched", spec, seen, "；".join(problems))
    return _ok("rule_matched", spec, seen, "规则命中情况符合预期")


def check_order_status(trace: Trace, spec: dict[str, str]) -> AssertionResult:
    """会话结束后订单的最终状态。"""
    after = trace.orders_after or {}
    problems, actual = [], {}
    for oid, want in spec.items():
        got = after.get(oid, {}).get("status", "MISSING")
        actual[oid] = got
        if got != want:
            problems.append(f"订单 {oid} 期望状态 {want}，实际 {got}")
    if problems:
        return _fail("order_status", spec, actual, "；".join(problems))
    return _ok("order_status", spec, actual, "订单最终状态符合预期")


def check_ledger_success_count(trace: Trace, spec: int) -> AssertionResult:
    records = trace.ledger_snapshot.get("records", []) or []
    got = sum(1 for r in records if r.get("outcome") == "SUCCESS")
    if got == spec:
        return _ok("ledger_success_count", spec, got, f"账本成功流水 {got} 条")
    return _fail("ledger_success_count", spec, got,
                 f"账本成功流水不符：期望 {spec} 条，实际 {got} 条")


def check_no_duplicate_payout(trace: Trace, spec: bool = True) -> AssertionResult:
    dup = trace.ledger_snapshot.get("duplicate_payouts", []) or []
    violated = bool(dup)
    if spec and violated:
        return _fail("no_duplicate_payout", True, dup,
                     f"检测到重复打款订单 {dup}")
    if not spec and not violated:
        return _ok("no_duplicate_payout", spec, dup, "无重复打款，符合预期")
    if spec and not violated:
        return _ok("no_duplicate_payout", True, [], "无重复打款")
    return _ok("no_duplicate_payout", spec, dup, "重复打款检测结果符合预期")


def check_turn_count(trace: Trace, spec: int) -> AssertionResult:
    got = len(trace.turns)
    if got == spec:
        return _ok("turn_count", spec, got, f"共 {got} 轮对话")
    return _fail("turn_count", spec, got, f"对话轮次不符：期望 {spec}，实际 {got}")


CHECKS: dict[str, Callable[[Trace, Any], AssertionResult]] = {
    "action": check_action,
    "turn_action": check_turn_action,
    "reply_contains_any": check_reply_contains_any,
    "reply_contains_all": check_reply_contains_all,
    "reply_not_contains": check_reply_not_contains,
    "reply_regex": check_reply_regex,
    "tool_count": check_tool_count,
    "tool_count_max": check_tool_count_max,
    "money_move_count": check_money_move_count,
    "timeout_count": check_timeout_count,
    "rule_matched": check_rule_matched,
    "order_status": check_order_status,
    "ledger_success_count": check_ledger_success_count,
    "no_duplicate_payout": check_no_duplicate_payout,
    "turn_count": check_turn_count,
}


def run_expectations(trace: Trace, expect: dict[str, Any]) -> list[AssertionResult]:
    """执行一组断言声明。未知类型不会被静默忽略，而是显式报错。"""
    results: list[AssertionResult] = []
    for kind, spec in expect.items():
        fn = CHECKS.get(kind)
        if fn is None:
            results.append(_fail(
                kind, spec, None,
                f"未知的断言类型 {kind!r}，可用：{', '.join(sorted(CHECKS))}"))
            continue
        try:
            results.append(fn(trace, spec))
        except Exception as exc:  # 断言自身出错也要暴露，不能当作通过
            results.append(_fail(kind, spec, None,
                                 f"断言执行异常：{type(exc).__name__}: {exc}"))
    return results
