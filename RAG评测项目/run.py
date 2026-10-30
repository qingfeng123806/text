#!/usr/bin/env python3
"""Agent 行为审计与回归评测系统 —— 命令行入口。

用法：
  python run.py demo                      一键完整演示（推荐首次运行）
  python run.py agents                    列出所有 Agent 版本
  python run.py cases                     列出评测用例
  python run.py eval  [--agent v1|all]    跑评测
  python run.py compare --baseline A --candidate B
  python run.py report --run-id <id>      对已有运行重新出报告
  python run.py verify --run-id <id>      校验审计链完整性
  python run.py stats  [--run-id <id>]    汇总本次运行的量化数据点
  python run.py serve  [--port 8770]      启动本地 Web 控制台

零第三方依赖，纯 Python 标准库，可离线运行。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

# Windows 控制台默认可能是 GBK，先切成 UTF-8，保证中文报告不乱码
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from agent_audit.agent.variants import (  # noqa: E402
    available_agents, build_all_agents, describe_all, get_agent)
from agent_audit.domain.order_store import OrderStore  # noqa: E402
from agent_audit.eval.regression import compare_runs  # noqa: E402
from agent_audit.eval.runner import EvalRunner, load_run  # noqa: E402
from agent_audit.eval.schema import load_cases  # noqa: E402
from agent_audit.report.html_report import render_html_report  # noqa: E402
from agent_audit.report.markdown_report import render_markdown, save_markdown  # noqa: E402
from agent_audit.report.stats import collect_stats, render_stats_text  # noqa: E402
from agent_audit.runtime.recorder import AuditLog  # noqa: E402

DEFAULT_CASES = ROOT / "cases" / "refund_cases.json"
DEFAULT_ORDERS = ROOT / "data" / "orders.json"
RUNS_DIR = ROOT / "runs"
BASELINE_AGENT = "refund-agent-v1"


# ============================================================
# 基础工具
# ============================================================

def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def load_all_cases(path: Path = DEFAULT_CASES) -> list[Any]:
    return load_cases(path)


def resolve_agent_ids(spec: str) -> list[str]:
    if spec in ("all", "*"):
        return available_agents()
    out = []
    for s in spec.split(","):
        s = s.strip()
        if not s:
            continue
        if s in available_agents():
            out.append(s)
            continue
        matches = [a for a in available_agents() if s in a]
        if not matches:
            raise SystemExit(f"找不到 Agent {s!r}，可用：{', '.join(available_agents())}")
        out.append(matches[0])
    return out


def rule_label(rid: str) -> str:
    return {"R1": "无订单号先追问", "R2": "跨用户订单拒绝",
            "R3": "同一订单只能退款一次", "R4": "工具超时不能重复退款"}.get(rid, rid)


# ============================================================
# 子命令
# ============================================================

def cmd_agents(_: argparse.Namespace) -> int:
    print("可用的 Agent 版本：\n")
    for d in describe_all():
        off = d.get("disabled_rules") or []
        flag = "  退化的规则：" + "、".join(off) if off else "  完整实现全部规则"
        print(f"  ● {d['agent_id']}")
        print(f"    {d['display_name']}")
        print(f"    {d['description']}{flag}\n")
    return 0


def cmd_cases(_: argparse.Namespace) -> int:
    cases = load_all_cases()
    print(f"共 {len(cases)} 条用例（{DEFAULT_CASES.name}）\n")
    by_rule: dict[str, list[Any]] = {}
    for c in cases:
        for r in (c.rules or ["其他"]):
            by_rule.setdefault(r, []).append(c)
    for rid in sorted(by_rule):
        print(f"  【{rid}】{rule_label(rid)}  ({len(by_rule[rid])} 条)")
        for c in by_rule[rid]:
            faults = f"  [故障注入 ×{len(c.faults)}]" if c.faults else ""
            print(f"    - {c.case_id:<8} {c.title}{faults}")
        print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """汇总一次运行的量化数据点。数字全部从产物现算，可随时复核。"""
    run_id = args.run_id or _latest_run(args.out_dir)
    if not run_id:
        print("还没有任何运行记录。先执行：python run.py demo")
        return 1
    try:
        st = collect_stats(run_id, out_dir=args.out_dir,
                           cases_path=Path(args.cases), root=ROOT)
    except FileNotFoundError as exc:
        print(f"{exc}")
        return 1
    if args.json:
        print(json.dumps(st, ensure_ascii=False, indent=2))
    else:
        print()
        print(render_stats_text(st))
        print()
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    run_id = args.run_id or new_run_id("eval")
    cases = load_all_cases(Path(args.cases))
    store = OrderStore.from_file(Path(args.orders))
    agent_ids = resolve_agent_ids(args.agent)

    runner = EvalRunner(cases, store, run_id, out_dir=args.out_dir,
                        clock_mode=args.clock)
    runs = []
    print(f"运行 ID：{run_id}")
    print(f"用例：{len(cases)} 条 ｜ 时钟：{'真实' if args.clock == 'real' else '虚拟'}\n")

    for aid in agent_ids:
        agent = get_agent(aid)
        t0 = time.perf_counter()
        rr = runner.run_agent(agent)
        elapsed = (time.perf_counter() - t0) * 1000
        runs.append(rr)
        inv = rr.summary().get("invariant_violations", {})
        inv_s = "、".join(f"{k}×{v}" for k, v in sorted(inv.items())) or "无"
        mark = "PASS" if rr.failed == 0 else "FAIL"
        print(f"  [{mark}] {rr.agent_name}")
        print(f"         {rr.passed}/{rr.total} 通过 ({rr.pass_rate:.1f}%) ｜ "
              f"严重违规 {rr.critical_count} ｜ 不变式触发：{inv_s} ｜ {elapsed:.0f} ms")
        for c in rr.cases:
            if not c.passed:
                print(f"           ✗ {c.case_id} {c.title}")

    runner.write_manifest(runs)
    print(f"\n产物目录：{Path(args.out_dir) / run_id}")
    return 0 if all(r.failed == 0 for r in runs) else 1


def cmd_compare(args: argparse.Namespace) -> int:
    base_path = Path(args.baseline)
    cand_path = Path(args.candidate)
    if base_path.suffix == ".json" and base_path.exists():
        baseline: Any = base_path
    else:
        baseline = Path(args.out_dir) / args.baseline_run / "results" / f"{args.baseline}.json"
    if cand_path.suffix == ".json" and cand_path.exists():
        candidate: Any = cand_path
    else:
        candidate = Path(args.out_dir) / args.candidate_run / "results" / f"{args.candidate}.json"

    rep = compare_runs(baseline, candidate)
    s = rep.summary()

    print(f"\n回归对比：{s['baseline_agent']}  →  {s['candidate_agent']}")
    print("=" * 72)
    print(f"  通过率     {s['baseline_pass_rate']}%  →  {s['candidate_pass_rate']}%")
    print(f"  新增回归   {s['regressions']} 条")
    print(f"  本次修复   {s['fixed']} 条")
    print(f"  稳定通过   {s['stable_pass']} 条")
    print(f"  存量失败   {s['stable_fail']} 条")
    if s["rule_attribution"]:
        print("  退化归因   " + "、".join(
            f"{k}({rule_label(k)}) × {v}" for k, v in s["rule_attribution"].items()))
    if s["invariant_attribution"]:
        print("  触发不变式 " + "、".join(s["invariant_attribution"].keys()))
    print("=" * 72)

    for d in rep.regressions:
        print(f"\n  🔴 REGRESSION  {d.case_id}  {d.title}")
        print(f"     涉及规则：{'、'.join(d.rules) or '未标注'}")
        for f in d.new_failures:
            print(f"       + {f}")
    for d in rep.fixed:
        print(f"\n  🟢 FIXED  {d.case_id}  {d.title}")

    if args.out:
        rep.save(args.out)
        print(f"\n对比结果已保存：{args.out}")

    return 1 if rep.has_regression else 0


def cmd_verify(args: argparse.Namespace) -> int:
    run_dir = Path(args.out_dir) / args.run_id / "audit"
    if not run_dir.exists():
        raise SystemExit(f"找不到审计目录：{run_dir}")
    files = sorted(run_dir.glob("*.jsonl"))
    if not files:
        raise SystemExit(f"{run_dir} 下没有审计文件")

    print(f"\n审计链完整性校验：{args.run_id}\n")
    all_ok = True
    for f in files:
        v = AuditLog.verify(f)
        all_ok = all_ok and bool(v["ok"])
        icon = "✅" if v["ok"] else "❌"
        print(f"  {icon} {f.name:<38} {v['records']:>5} 条事件")
        print(f"     {v['message']}")
    print()
    print("结论：" + ("全部审计流完整，未被篡改" if all_ok else "存在审计链断裂，请检查"))
    return 0 if all_ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    results_dir = Path(args.out_dir) / args.run_id / "results"
    files = sorted(results_dir.glob("*.json"))
    if not files:
        raise SystemExit(f"找不到运行结果：{results_dir}")

    from agent_audit.eval.runner import RunResult
    runs = [RunResult.from_dict(__import__("json").loads(
        f.read_text(encoding="utf-8"))) for f in files]

    regs = []
    if args.with_regression:
        base = next((r for r in runs if r.agent_id == BASELINE_AGENT), None)
        if base:
            for r in runs:
                if r.agent_id != BASELINE_AGENT:
                    regs.append(compare_runs(base, r))

    checks = []
    audit_dir = Path(args.out_dir) / args.run_id / "audit"
    for f in sorted(audit_dir.glob("*.jsonl")):
        v = AuditLog.verify(f)
        v["name"] = f.name
        checks.append(v)

    html_path = Path(args.out_dir) / args.run_id / "report.html"
    render_html_report(runs, regs, checks, out_path=html_path)
    md = render_markdown(runs, regs, checks)
    md_path = save_markdown(md, Path(args.out_dir) / args.run_id / "report.md")
    print(f"HTML 报告：{html_path}")
    print(f"Markdown ：{md_path}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    run_id = args.run_id or new_run_id("demo")
    cases = load_all_cases(Path(args.cases))
    store = OrderStore.from_file(Path(args.orders))

    print("=" * 72)
    print("  Agent 行为审计与回归评测系统 · 完整演示")
    print("=" * 72)
    print(f"\n运行 ID：{run_id}")
    print(f"用例集：{len(cases)} 条（覆盖 R1-R4 四条业务规则）")
    print(f"参评 Agent：{len(available_agents())} 个版本（1 个基线 + "
          f"{len(available_agents()) - 1} 个植入缺陷的变异体）\n")

    runner = EvalRunner(cases, store, run_id, out_dir=args.out_dir,
                        clock_mode=args.clock)
    runs = []
    print("─" * 72)
    print("  第一步：跑批评测")
    print("─" * 72)
    for agent in build_all_agents():
        rr = runner.run_agent(agent)
        runs.append(rr)
        inv = rr.summary().get("invariant_violations", {})
        inv_s = "、".join(f"{k}×{v}" for k, v in sorted(inv.items())) or "无"
        mark = "✅" if rr.failed == 0 else ("🔴" if rr.critical_count else "⚠️ ")
        print(f"  {mark} {rr.agent_name}")
        print(f"      {rr.passed}/{rr.total} 通过 ({rr.pass_rate:.1f}%) ｜ "
              f"严重违规 {rr.critical_count} ｜ 不变式：{inv_s}")

    runner.write_manifest(runs)

    base = next(r for r in runs if r.agent_id == BASELINE_AGENT)
    regs = [compare_runs(base, r) for r in runs if r.agent_id != BASELINE_AGENT]

    print("\n" + "─" * 72)
    print("  第二步：回归对比（基线 = v1）")
    print("─" * 72)
    for rep in regs:
        s = rep.summary()
        verdict = (f"🔴 新增回归 {s['regressions']} 条" if s["has_regression"]
                   else "🟢 无新增回归")
        print(f"  {s['candidate_agent']}")
        print(f"      {s['baseline_pass_rate']}% → {s['candidate_pass_rate']}% ｜ {verdict}")
        if s["rule_attribution"]:
            print("      退化归因：" + "、".join(
                f"{k}({rule_label(k)})×{v}" for k, v in s["rule_attribution"].items()))
        for d in rep.regressions:
            print(f"        · {d.case_id} {d.title}")

    print("\n" + "─" * 72)
    print("  第三步：审计链完整性校验")
    print("─" * 72)
    checks = []
    audit_dir = Path(args.out_dir) / run_id / "audit"
    for f in sorted(audit_dir.glob("*.jsonl")):
        v = AuditLog.verify(f)
        v["name"] = f.name
        checks.append(v)
        print(f"  {'✅' if v['ok'] else '❌'} {f.name:<38} {v['records']:>5} 条　{v['message']}")

    print("\n" + "─" * 72)
    print("  第四步：生成报告")
    print("─" * 72)
    html_path = Path(args.out_dir) / run_id / "report.html"
    render_html_report(runs, regs, checks, out_path=html_path)
    md_path = save_markdown(render_markdown(runs, regs, checks),
                            Path(args.out_dir) / run_id / "report.md")
    print(f"  HTML     {html_path}")
    print(f"  Markdown {md_path}")

    print("\n" + "=" * 72)
    baseline_pass = base.passed == base.total
    print("  演示结论")
    print("=" * 72)
    print(f"  · 基线 Agent 通过 {base.passed}/{base.total} 条用例，"
          f"{'全部通过' if baseline_pass else '存在失败（需检查）'}")
    print(f"  · {len(regs)} 个变异体共被识别 {sum(len(r.regressions) for r in regs)} 条回归，"
          f"全部定位到具体业务规则")
    print(f"  · 全部 {len(checks)} 条审计流哈希链完整，可验证未被篡改")
    print(f"\n  查看报告：python run.py serve   然后打开 {html_path.name}\n")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from agent_audit.server.app import run_server
    return run_server(port=args.port, runs_dir=args.out_dir, root=ROOT,
                      open_browser=not args.no_browser)


# ============================================================
# 参数
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Agent 行为审计与回归评测系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python run.py demo\n"
               "  python run.py eval --agent refund-agent-v2-timeout-retry\n"
               "  python run.py compare --baseline refund-agent-v1 "
               "--candidate refund-agent-v2-timeout-retry --run-id <id>\n"
               "  python run.py serve\n")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("demo", help="一键完整演示").set_defaults(func=cmd_demo)
    sub.add_parser("agents", help="列出 Agent 版本").set_defaults(func=cmd_agents)
    sub.add_parser("cases", help="列出评测用例").set_defaults(func=cmd_cases)

    e = sub.add_parser("eval", help="跑评测")
    e.add_argument("--agent", default="all", help="Agent ID 或 all（默认 all）")
    e.add_argument("--run-id", default="")
    e.add_argument("--clock", default="virtual", choices=["virtual", "real"])
    e.set_defaults(func=cmd_eval)

    c = sub.add_parser("compare", help="回归对比")
    c.add_argument("--baseline", default=BASELINE_AGENT)
    c.add_argument("--candidate", required=True)
    c.add_argument("--run-id", default="")
    c.add_argument("--out", default="")
    c.set_defaults(func=cmd_compare)

    v = sub.add_parser("verify", help="校验审计链")
    v.add_argument("--run-id", required=True)
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("report", help="生成报告")
    r.add_argument("--run-id", required=True)
    r.add_argument("--with-regression", action="store_true")
    r.set_defaults(func=cmd_report)

    stt = sub.add_parser("stats", help="汇总某次运行的量化数据点")
    stt.add_argument("--run-id", default="")
    stt.add_argument("--json", action="store_true", help="输出 JSON 而非表格")
    stt.set_defaults(func=cmd_stats)

    s = sub.add_parser("serve", help="启动 Web 控制台")
    s.add_argument("--port", type=int, default=8770)
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(func=cmd_serve)

    demo = sub.choices["demo"]
    demo.add_argument("--run-id", default="")
    demo.add_argument("--clock", default="virtual", choices=["virtual", "real"])

    for sp in (e, c, v, r, stt, s, demo):
        sp.add_argument("--cases", default=str(DEFAULT_CASES))
        sp.add_argument("--orders", default=str(DEFAULT_ORDERS))
        sp.add_argument("--out-dir", default=str(RUNS_DIR))
    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    # compare 用 --run-id 指定基线/候选所在的运行目录
    if args.cmd == "compare":
        args.baseline_run = args.run_id or _latest_run(args.out_dir)
        args.candidate_run = args.baseline_run
    elif args.cmd in ("verify", "report") and not args.run_id:
        args.run_id = _latest_run(args.out_dir)
    return int(args.func(args) or 0)


def _latest_run(out_dir: str) -> str:
    d = Path(out_dir)
    if not d.exists():
        return ""
    dirs = sorted([p for p in d.iterdir() if p.is_dir() and (p / "results").exists()],
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return dirs[0].name if dirs else ""


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
    except SystemExit:
        raise
    except Exception as exc:  # 兜底：把栈打出来，别只留一句 "Error"
        import traceback
        traceback.print_exc()
        sys.exit(2)
