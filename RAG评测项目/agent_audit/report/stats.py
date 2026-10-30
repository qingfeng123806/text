"""把一次运行的产物汇总成可核对的量化数据点。

这里的每一个数字都从 runs/<run_id>/ 下的产物直接算出，任何人都可以用
`python run.py stats --run-id <id>` 重算一遍核对。

刻意不提供「准确率」这类指标：本项目的判定结果是用例通过/失败与不变式
是否成立，是确定性的计数，不是统计意义上的准确率/召回率。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from ..agent.variants import baseline_agent
from ..eval.invariants import INVARIANTS
from ..eval.regression import compare_runs
from ..eval.runner import RunResult
from ..eval.schema import Case, load_cases
from ..runtime.recorder import AuditLog

BASELINE_AGENT = baseline_agent().agent_id


def _case_stats(cases: list[Case]) -> dict[str, Any]:
    by_rule: Counter[str] = Counter()
    for c in cases:
        for r in (c.rules or ["-"]):
            by_rule[r] += 1
    fault_kinds: Counter[str] = Counter()
    for c in cases:
        for f in c.faults:
            fault_kinds[str(f.get("kind", "unknown"))] += 1
    return {
        "total": len(cases),
        "by_rule": dict(sorted(by_rule.items())),
        "by_severity": dict(Counter(c.severity for c in cases)),
        "multi_turn": sum(1 for c in cases if len(c.messages) > 1),
        "single_turn": sum(1 for c in cases if len(c.messages) <= 1),
        "with_faults": sum(1 for c in cases if c.faults),
        "fault_kinds": dict(sorted(fault_kinds.items())),
        "expected_assertions": sum(len(c.expect) for c in cases),
        "users": dict(sorted(Counter(c.user_id for c in cases).items())),
    }


def _agent_row(data: dict[str, Any]) -> dict[str, Any]:
    s = data["summary"]
    hits: Counter[str] = Counter()
    for c in data.get("cases", []):
        for f in c.get("failed_checks", []):
            if str(f).startswith("INV-"):
                hits[str(f).split("(")[0]] += 1
    return {
        "agent_id": s["agent_id"],
        "agent_name": s.get("agent_name", s["agent_id"]),
        "passed": s["passed"],
        "total": s["total"],
        "failed": s["failed"],
        "pass_rate": s["pass_rate"],
        "critical": s["critical_violations"],
        "invariant_hits": dict(sorted(hits.items())),
        "by_rule": s.get("by_rule", {}),
    }


def _trace_stats(data: dict[str, Any]) -> dict[str, Any]:
    tools: Counter[str] = Counter()
    money = timeouts = errors = turns = 0
    for t in data.get("traces", []):
        turns += len(t.get("turns", []))
        for tu in t.get("turns", []):
            for c in (tu.get("reply") or {}).get("tool_calls", []):
                tools[c.get("tool", "?")] += 1
                if c.get("money_moved"):
                    money += 1
                if c.get("status") == "TIMEOUT":
                    timeouts += 1
                elif c.get("status") == "ERROR":
                    errors += 1
    return {
        "calls": sum(tools.values()),
        "by_tool": dict(sorted(tools.items())),
        "money_moves": money,
        "timeouts": timeouts,
        "errors": errors,
        "turns": turns,
    }


def _audit_stats(run_dir: Path) -> dict[str, Any]:
    d = run_dir / "audit"
    if not d.exists():
        return {"streams": 0, "events": 0, "by_type": {}, "ok": True, "archived": 0}
    by_type: Counter[str] = Counter()
    events = 0
    ok = True
    streams = 0
    for f in sorted(d.glob("*.jsonl")):
        streams += 1
        v = AuditLog.verify(f)
        ok = ok and bool(v["ok"])
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events += 1
                by_type[json.loads(line).get("type", "?")] += 1
    archived = len(list((d / "archive").glob("*.jsonl"))) if (d / "archive").exists() else 0
    return {
        "streams": streams,
        "events": events,
        "by_type": dict(sorted(by_type.items())),
        "ok": ok,
        "archived": archived,
    }


def collect_stats(run_id: str, out_dir: str | Path = "runs",
                  cases_path: Optional[str | Path] = None,
                  root: str | Path = ".") -> dict[str, Any]:
    root_p = Path(root).resolve()
    run_dir = Path(out_dir) / run_id
    cases_file = Path(cases_path) if cases_path else root_p / "cases" / "refund_cases.json"
    cases = load_cases(cases_file)

    results = {}
    for p in sorted((run_dir / "results").glob("*.json")):
        results[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    if not results:
        raise FileNotFoundError(f"该运行下没有结果文件：{run_dir / 'results'}")

    # 统计口径以基线为准：它代表"正确实现"跑一遍的规模
    ref_id = BASELINE_AGENT if BASELINE_AGENT in results else sorted(results)[0]
    ref = results[ref_id]

    # 回归对比
    base = RunResult.from_dict(results[BASELINE_AGENT]) if BASELINE_AGENT in results else None
    per_candidate: list[dict[str, Any]] = []
    totals: Counter[str] = Counter()
    for aid in sorted(results):
        if base is None or aid == BASELINE_AGENT:
            continue
        s = compare_runs(base, RunResult.from_dict(results[aid])).summary()
        per_candidate.append({
            "agent_id": aid,
            "regressions": s["regressions"],
            "fixed": s["fixed"],
            "stable_pass": s["stable_pass"],
            "stable_fail": s["stable_fail"],
            "baseline_pass_rate": s["baseline_pass_rate"],
            "candidate_pass_rate": s["candidate_pass_rate"],
            "rule_attribution": s["rule_attribution"],
        })
        for k in ("regressions", "fixed", "stable_pass", "stable_fail"):
            totals[k] += s[k]
        for r, n in s["rule_attribution"].items():
            totals[f"rule:{r}"] += n

    return {
        "run_id": run_id,
        "cases": _case_stats(cases),
        "invariants": {
            "registered": len(INVARIANTS),
            "ids": [i.inv_id for i in INVARIANTS],
            "by_severity": dict(Counter(i.severity.value for i in INVARIANTS)),
        },
        "agents": [_agent_row(results[a]) for a in sorted(results)],
        "checkpoints": {
            "reference_agent": ref_id,
            "assertions": sum(len(c.get("assertions", []))
                              for c in ref.get("cases", [])),
            "invariant_checks": sum(len(c.get("invariants", []))
                                    for c in ref.get("cases", [])),
        },
        "tool_calls": _trace_stats(ref),
        "regression": {
            "baseline": BASELINE_AGENT,
            "candidates": per_candidate,
            "totals": dict(sorted(totals.items())),
        },
        "audit": _audit_stats(run_dir),
    }


# ------------------------------------------------------------------
# 渲染
# ------------------------------------------------------------------

def render_stats_text(st: dict[str, Any]) -> str:
    L: list[str] = []
    add = L.append
    bar = "─" * 74

    c = st["cases"]
    add(f"运行：{st['run_id']}")
    add(bar)
    add("用例集")
    add(f"  用例总数            {c['total']}")
    add(f"  按规则              " + "  ".join(f"{k}:{v}" for k, v in c["by_rule"].items()))
    add(f"  多轮 / 单轮         {c['multi_turn']} / {c['single_turn']}")
    add(f"  含故障注入          {c['with_faults']} 条"
        + (f"（{'  '.join(f'{k}×{v}' for k, v in c['fault_kinds'].items())}）"
           if c["fault_kinds"] else ""))
    add(f"  严重级别            " + "  ".join(f"{k}:{v}" for k, v in c["by_severity"].items()))
    add(f"  期望断言项          {c['expected_assertions']}")
    add(f"  涉及用户            " + "  ".join(f"{k}:{v}" for k, v in c["users"].items()))

    iv = st["invariants"]
    add("")
    add("全局不变式")
    add(f"  注册条数            {iv['registered']}（{'、'.join(iv['ids'])}）")
    add(f"  按级别              " + "  ".join(f"{k}:{v}" for k, v in iv["by_severity"].items()))

    ck = st["checkpoints"]
    add("")
    add(f"校验点（以 {ck['reference_agent']} 单次运行计）")
    add(f"  用例断言            {ck['assertions']}")
    add(f"  不变式检查          {ck['invariant_checks']}")
    add(f"  合计                {ck['assertions'] + ck['invariant_checks']}")

    tc = st["tool_calls"]
    add("")
    add(f"工具调用（{ck['reference_agent']} 单次运行）")
    add(f"  调用总数            {tc['calls']}"
        + (f"（{'  '.join(f'{k}:{v}' for k, v in tc['by_tool'].items())}）" if tc["by_tool"] else ""))
    add(f"  真实动钱            {tc['money_moves']} 次")
    add(f"  超时 / 报错         {tc['timeouts']} / {tc['errors']}")
    add(f"  对话轮次            {tc['turns']}")

    add("")
    add("各 Agent 版本")
    for a in st["agents"]:
        hits = "  ".join(f"{k}×{v}" for k, v in a["invariant_hits"].items()) or "—"
        add(f"  {a['agent_id']:<30} {a['passed']:>2}/{a['total']}  "
            f"{a['pass_rate']:>6.2f}%   严重违规 {a['critical']}   触发不变式 {hits}")

    rg = st["regression"]
    add("")
    add(f"回归对比（基线 {rg['baseline']}）")
    for r in rg["candidates"]:
        attr = "  ".join(f"{k}×{v}" for k, v in sorted(r["rule_attribution"].items())) or "—"
        add(f"  {r['agent_id']:<30} 通过率 {r['baseline_pass_rate']}% → {r['candidate_pass_rate']}%   "
            f"新增回归 {r['regressions']:>2}   修复 {r['fixed']}   "
            f"稳定通过 {r['stable_pass']:>2}   存量失败 {r['stable_fail']}")
        add(f"  {'':<30} 归因：{attr}")
    if rg["totals"]:
        add("  合计                " + "  ".join(
            f"{k}:{v}" for k, v in rg["totals"].items()))
        attr_sum = sum(v for k, v in rg["totals"].items() if k.startswith("rule:"))
        n_reg = rg["totals"].get("regressions", 0)
        if attr_sum != n_reg:
            add(f"  口径说明            归因合计 {attr_sum} 大于回归条数 {n_reg}："
                "一条用例可同时归属多条规则，会被分别计入各规则。")

    au = st["audit"]
    add("")
    add("审计")
    add(f"  审计流              {au['streams']}")
    add(f"  事件总数            {au['events']}"
        + (f"（{'  '.join(f'{k}:{v}' for k, v in au['by_type'].items())}）" if au["by_type"] else ""))
    add(f"  哈希链校验          {'全部通过' if au['ok'] else '存在断裂'}")
    if au["archived"]:
        add(f"  归档的旧链          {au['archived']} 条（重跑同一 run_id 时保留）")

    return "\n".join(L)
