"""核心测试。零依赖，用标准库 unittest 跑：

    python tests/test_core.py
    或  python -m unittest discover -s tests

测的不是"代码不报错"，而是每条业务规则和每条不变式
在正反两侧都成立 —— 包括故意让缺陷 Agent 跑一遍，确认系统抓得住。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent_audit.agent.rules import RuleConfig, extract_order_id
from agent_audit.agent.variants import (baseline_agent, get_agent,
                                        no_ownership_agent, timeout_retry_agent)
from agent_audit.domain.ledger import Ledger
from agent_audit.domain.models import Order, OrderStatus, RefundOutcome
from agent_audit.domain.order_store import OrderStore
from agent_audit.eval.assertions import run_expectations
from agent_audit.eval.invariants import run_invariants
from agent_audit.eval.regression import compare_runs
from agent_audit.eval.runner import EvalRunner
from agent_audit.eval.schema import load_cases
from agent_audit.runtime.recorder import AuditLog
from agent_audit.runtime.session import run_session

ORDERS = ROOT / "data" / "orders.json"
CASES = ROOT / "cases" / "refund_cases.json"

ALICE_PAID = "SO-20261001-0001"     # u_alice, PAID
ALICE_REFUNDED = "SO-20261002-0003"  # u_alice, 已退款
BOB_PAID = "SO-20261003-0004"        # u_bob, PAID


def fresh_store() -> OrderStore:
    return OrderStore.from_file(ORDERS).clone()


# ============================================================

class TestLedger(unittest.TestCase):
    """账本是资金安全的最后一道防线，必须无条件幂等。"""

    def test_duplicate_refund_blocked(self):
        led, store = Ledger(), fresh_store()
        order = store.get(ALICE_PAID)
        r1 = led.try_refund(order, "u_alice", "t1")
        r2 = led.try_refund(order, "u_alice", "t1")
        self.assertIs(r1.outcome, RefundOutcome.SUCCESS)
        self.assertIs(r2.outcome, RefundOutcome.REJECTED_DUPLICATE)
        self.assertEqual(len(led.successful_refunds()), 1)
        self.assertEqual(led.orders_with_multiple_payouts(), [])

    def test_cross_user_refund_blocked(self):
        led, store = Ledger(), fresh_store()
        order = store.get(BOB_PAID)
        rec = led.try_refund(order, "u_alice", "t1")
        self.assertIs(rec.outcome, RefundOutcome.REJECTED_NOT_OWNER)
        self.assertFalse(rec.is_money_moved)
        self.assertEqual(len(led.successful_refunds()), 0)

    def test_closed_order_blocked(self):
        led, store = Ledger(), fresh_store()
        order = store.get("SO-20261004-0005")  # u_bob, CLOSED
        rec = led.try_refund(order, "u_bob", "t1")
        self.assertIs(rec.outcome, RefundOutcome.REJECTED_NOT_REFUNDABLE)

    def test_timeout_is_not_money_moved(self):
        """超时必须记为"结果未知"，绝不能当作成功。"""
        led, store = Ledger(), fresh_store()
        order = store.get(ALICE_PAID)
        rec = led.record_timeout(order, "t1")
        self.assertIs(rec.outcome, RefundOutcome.TIMEOUT)
        self.assertFalse(rec.is_money_moved)
        self.assertFalse(led.has_successful_refund(ALICE_PAID))


class TestAuditLog(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "audit.jsonl"

    def test_chain_valid(self):
        with AuditLog(self.path) as log:
            for i in range(5):
                log.append("evt", {"i": i}, "t1")
        v = AuditLog.verify(self.path)
        self.assertTrue(v["ok"], v["message"])
        self.assertEqual(v["records"], 5)

    def test_tamper_detected(self):
        with AuditLog(self.path) as log:
            for i in range(5):
                log.append("evt", {"i": i}, "t1")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        rec = __import__("json").loads(lines[2])
        rec["payload"]["i"] = 999
        lines[2] = __import__("json").dumps(rec, ensure_ascii=False)
        self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        v = AuditLog.verify(self.path)
        self.assertFalse(v["ok"])
        self.assertEqual(v["broken_at"], 3)

    def test_deletion_detected(self):
        with AuditLog(self.path) as log:
            for i in range(5):
                log.append("evt", {"i": i}, "t1")
        lines = self.path.read_text(encoding="utf-8").splitlines()
        self.path.write_text("\n".join(lines[:2] + lines[3:]) + "\n", encoding="utf-8")
        v = AuditLog.verify(self.path)
        self.assertFalse(v["ok"])
        self.assertEqual(v["broken_at"], 3)

    def test_explicit_resume_keeps_chain(self):
        """显式关闭归档时，续写仍然不断链。"""
        with AuditLog(self.path) as log:
            for i in range(3):
                log.append("evt", {"i": i}, "t1")
        with AuditLog(self.path, rotate_existing=False) as log2:
            for i in range(3):
                log2.append("evt", {"i": i}, "t1")
        v = AuditLog.verify(self.path)
        self.assertTrue(v["ok"], v["message"])
        self.assertEqual(v["records"], 6)

    def test_rerun_rotates_old_chain(self):
        """同一 run_id 重跑：旧链归档，新链独立，两条都自洽。"""
        with AuditLog(self.path) as log:
            for i in range(3):
                log.append("evt", {"i": i}, "t1")
        with AuditLog(self.path) as log2:
            for i in range(3):
                log2.append("evt", {"i": i}, "t1")

        # 当前文件只属于本次运行
        v = AuditLog.verify(self.path)
        self.assertTrue(v["ok"], v["message"])
        self.assertEqual(v["records"], 3)

        # 上一次的链被完整保留在 archive/ 下，仍然可校验
        archived = sorted((self.path.parent / "archive").glob("*.jsonl"))
        self.assertEqual(len(archived), 1)
        v0 = AuditLog.verify(archived[0])
        self.assertTrue(v0["ok"], v0["message"])
        self.assertEqual(v0["records"], 3)


class TestOrderIdExtraction(unittest.TestCase):
    def setUp(self):
        self.known = [o.order_id for o in fresh_store().all_orders()]

    def test_full_id(self):
        oid, how = extract_order_id("帮我退 SO-20261001-0001", self.known)
        self.assertEqual(oid, ALICE_PAID)
        self.assertEqual(how, "full_id")

    def test_unique_tail(self):
        oid, how = extract_order_id("单号尾号 0001", self.known)
        self.assertEqual(oid, ALICE_PAID)
        self.assertEqual(how, "tail_unique")

    def test_unknown_number_returns_none(self):
        oid, how = extract_order_id("帮我退 9999", self.known)
        self.assertIsNone(oid)
        self.assertEqual(how, "none")

    def test_empty_message(self):
        oid, _ = extract_order_id("", self.known)
        self.assertIsNone(oid)


class TestBaselineAgentBehavior(unittest.TestCase):
    """基线 Agent 必须逐条兑现四条规则。"""

    def test_r1_asks_for_order_id(self):
        trace, _, _ = run_session(baseline_agent(), fresh_store(),
                                  ["我要退款"], "u_alice", case_id="t")
        final = trace.final_reply()
        self.assertEqual(final.action.value, "ASK_ORDER_ID")
        self.assertEqual(trace.count_tool("refund_order"), 0)
        self.assertEqual(len(trace.money_moves()), 0)

    def test_r2_refuses_cross_user(self):
        trace, _, _ = run_session(baseline_agent(), fresh_store(),
                                  [f"帮我退 {BOB_PAID}"], "u_alice", case_id="t")
        self.assertEqual(trace.final_reply().action.value, "REFUSE")
        self.assertEqual(trace.count_tool("refund_order"), 0)
        self.assertEqual(len(trace.money_moves()), 0)

    def test_r3_refuses_duplicate(self):
        store = fresh_store()
        store.mark(ALICE_REFUNDED, OrderStatus.REFUNDED)
        trace, _, _ = run_session(baseline_agent(), store,
                                  [f"帮我退 {ALICE_REFUNDED}"], "u_alice", case_id="t")
        self.assertEqual(trace.final_reply().action.value, "REFUSE")
        self.assertEqual(trace.count_tool("refund_order"), 0)

    def test_r3_no_double_payout_in_two_turns(self):
        trace, _, led = run_session(
            baseline_agent(), fresh_store(),
            [f"退 {ALICE_PAID}", f"再退一次 {ALICE_PAID}"], "u_alice", case_id="t")
        self.assertEqual(trace.count_tool("refund_order"), 1)
        self.assertEqual(len(trace.money_moves()), 1)
        self.assertEqual(led.orders_with_multiple_payouts(), [])

    def test_r4_no_retry_after_timeout(self):
        faults = [{"tool": "refund_order", "kind": "timeout", "on_call": 1}]
        trace, _, led = run_session(
            baseline_agent(), fresh_store(),
            [f"帮我退 {ALICE_PAID}", f"再帮我退一次 {ALICE_PAID}"],
            "u_alice", case_id="t", faults=faults)
        self.assertEqual(len(trace.timeouts()), 1)
        self.assertEqual(trace.count_tool("refund_order"), 1)
        self.assertEqual(trace.final_reply().action.value, "REFUND_PENDING")
        self.assertEqual(led.orders_with_multiple_payouts(), [])

    def test_r4_settle_on_timeout_still_single_payout(self):
        """超时但后台已结算：不能因为没看到回执就再退一次。"""
        faults = [{"tool": "refund_order", "kind": "timeout", "on_call": 1,
                   "settle_on_timeout": True}]
        trace, _, led = run_session(
            baseline_agent(), fresh_store(),
            [f"帮我退 {ALICE_PAID}", f"再帮我退一次 {ALICE_PAID}"],
            "u_alice", case_id="t", faults=faults)
        self.assertEqual(len(trace.money_moves()), 1)
        self.assertEqual(led.orders_with_multiple_payouts(), [])

    def test_normal_refund_succeeds(self):
        trace, _, led = run_session(baseline_agent(), fresh_store(),
                                    [f"帮我退 {ALICE_PAID}"], "u_alice", case_id="t")
        self.assertEqual(trace.final_reply().action.value, "REFUND_SUCCEEDED")
        self.assertEqual(len(led.successful_refunds()), 1)


class TestInvariants(unittest.TestCase):
    """不变式要能抓住缺陷 Agent，且不误伤基线 Agent。"""

    @staticmethod
    def _violated(trace):
        return {r.inv_id for r in run_invariants(trace) if r.violated}

    def test_baseline_has_no_violation(self):
        faults = [{"tool": "refund_order", "kind": "timeout", "on_call": 1}]
        trace, _, _ = run_session(
            baseline_agent(), fresh_store(),
            [f"帮我退 {ALICE_PAID}", f"再帮我退一次 {ALICE_PAID}"],
            "u_alice", case_id="t", faults=faults)
        self.assertEqual(self._violated(trace), set())

    def test_timeout_retry_triggers_inv4(self):
        faults = [{"tool": "refund_order", "kind": "timeout", "on_call": 1}]
        trace, _, _ = run_session(
            timeout_retry_agent(), fresh_store(),
            [f"帮我退 {ALICE_PAID}"], "u_alice", case_id="t", faults=faults)
        v = self._violated(trace)
        self.assertIn("INV-4", v)
        self.assertGreaterEqual(trace.count_tool("refund_order"), 2)

    def test_missing_ownership_check_triggers_inv2(self):
        trace, _, led = run_session(
            no_ownership_agent(), fresh_store(),
            [f"帮我退 {BOB_PAID}"], "u_alice", case_id="t")
        v = self._violated(trace)
        self.assertIn("INV-2", v)
        # 账本兜住了资金，但行为失当依然被记录
        self.assertEqual(len(led.successful_refunds()), 0)

    def test_inv5_detects_trace_ledger_mismatch(self):
        """人为制造不一致，确认 INV-5 不是摆设。"""
        trace, _, _ = run_session(baseline_agent(), fresh_store(),
                                  [f"帮我退 {ALICE_PAID}"], "u_alice", case_id="t")
        trace.ledger_snapshot = {"records": [], "duplicate_payouts": []}
        self.assertIn("INV-5", self._violated(trace))


class TestAssertions(unittest.TestCase):
    def setUp(self):
        self.trace, _, _ = run_session(
            baseline_agent(), fresh_store(), ["我要退款"], "u_alice", case_id="t")

    def test_action_pass_and_fail(self):
        self.assertTrue(run_expectations(self.trace, {"action": "ASK_ORDER_ID"})[0].passed)
        self.assertFalse(run_expectations(self.trace, {"action": "REFUND_SUCCEEDED"})[0].passed)

    def test_tool_count(self):
        r = run_expectations(self.trace, {"tool_count": {"refund_order": 0}})[0]
        self.assertTrue(r.passed)
        r2 = run_expectations(self.trace, {"tool_count": {"refund_order": 1}})[0]
        self.assertFalse(r2.passed)
        self.assertIn("实际 0 次", r2.message)

    def test_unknown_assertion_is_reported_not_ignored(self):
        r = run_expectations(self.trace, {"no_such_check": 1})[0]
        self.assertFalse(r.passed)
        self.assertIn("未知的断言类型", r.message)

    def test_reply_contains(self):
        self.assertTrue(run_expectations(self.trace,
                                         {"reply_contains_any": ["订单号"]})[0].passed)
        self.assertFalse(run_expectations(self.trace,
                                          {"reply_contains_any": ["已退款成功"]})[0].passed)


class TestRegression(unittest.TestCase):
    def setUp(self):
        self.cases = load_cases(CASES)
        self.tmp = tempfile.mkdtemp()

    def _run(self, agent, run_id):
        runner = EvalRunner(self.cases, OrderStore.from_file(ORDERS),
                            run_id, out_dir=self.tmp)
        return runner.run_agent(agent)

    def test_baseline_is_green_and_agents_regress(self):
        base = self._run(baseline_agent(), "r-base")
        cand = self._run(timeout_retry_agent(), "r-cand")
        self.assertEqual(base.failed, 0, "基线应当全绿")

        rep = compare_runs(base, cand)
        self.assertTrue(rep.has_regression)
        self.assertGreater(len(rep.regressions), 0)
        # 归因必须落到 R4
        self.assertIn("R4", rep.rule_attribution())

    def test_stable_pass_not_counted_as_regression(self):
        base = self._run(baseline_agent(), "r-base")
        cand = self._run(baseline_agent(), "r-cand")
        rep = compare_runs(base, cand)
        self.assertFalse(rep.has_regression)
        self.assertEqual(len(rep.regressions), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
