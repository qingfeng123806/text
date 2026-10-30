"""Markdown 报告：可以贴进 PR、发到群里、塞进 CI 摘要。

HTML 报告适合人看，Markdown 适合贴到需要 diff 的地方。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..eval.regression import RegressionReport
from ..eval.runner import RunResult


def _bar(rate: float, width: int = 20) -> str:
    filled = int(round(rate / 100 * width))
    return "█" * filled + "░" * (width - filled)


def render_markdown(
    runs: list[RunResult],
    regressions: Optional[list[RegressionReport]] = None,
    audit_checks: Optional[list[dict[str, Any]]] = None,
) -> str:
    lines: list[str] = []
    lines.append("# Agent 行为审计与回归评测报告")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # ---------- 总览 ----------
    lines.append("## 一、总览")
    lines.append("")
    lines.append("| Agent | 通过 / 总数 | 通过率 | 严重违规(CRITICAL) |")
    lines.append("|---|---:|---:|---:|")
    for r in runs:
        flag = "✅" if r.failed == 0 else ("❌" if r.critical_count else "⚠️")
        lines.append(
            f"| {flag} {r.agent_name} | {r.passed} / {r.total} | "
            f"{r.pass_rate:.1f}% `{_bar(r.pass_rate)}` | {r.critical_count} |")
    lines.append("")

    # ---------- 逐 Agent 明细 ----------
    lines.append("## 二、失败明细")
    lines.append("")
    any_fail = False
    for r in runs:
        failures = [c for c in r.cases if not c.passed]
        if not failures:
            continue
        any_fail = True
        lines.append(f"### {r.agent_name}（{r.passed}/{r.total}）")
        lines.append("")
        for c in failures:
            rules = "、".join(c.rules) if c.rules else "未标注"
            sev = "🔴" if c.critical_count else "🟠"
            lines.append(f"- {sev} **{c.case_id}** {c.title}　（规则 {rules}）")
            for f in c.failed_checks:
                lines.append(f"  - `{f}`")
        lines.append("")

    if not any_fail:
        lines.append("全部 Agent 的所有用例均通过。")
        lines.append("")

    # ---------- 不变式 ----------
    lines.append("## 三、全局不变式违规统计")
    lines.append("")
    lines.append("不变式对所有用例无条件生效，不依赖用例是否写了断言。")
    lines.append("")
    inv_total: dict[str, int] = {}
    for r in runs:
        for inv, n in r.summary().get("invariant_violations", {}).items():
            inv_total[inv] = inv_total.get(inv, 0) + n
    if inv_total:
        lines.append("| 不变式 | 违规次数 |")
        lines.append("|---|---:|")
        for inv, n in sorted(inv_total.items()):
            lines.append(f"| {inv} | {n} |")
    else:
        lines.append("未发现不变式违规。")
    lines.append("")

    # ---------- 回归 ----------
    if regressions:
        lines.append("## 四、回归对比")
        lines.append("")
        for rep in regressions:
            s = rep.summary()
            verdict = "🔴 存在回归，建议拦截" if s["has_regression"] else "🟢 无新增回归"
            lines.append(f"### {s['baseline_agent']} → {s['candidate_agent']}：{verdict}")
            lines.append("")
            lines.append(
                f"- 基线通过率 {s['baseline_pass_rate']}% → 当前 {s['candidate_pass_rate']}%")
            lines.append(
                f"- 新增回归 {s['regressions']} 条 / 修复 {s['fixed']} 条 / "
                f"稳定通过 {s['stable_pass']} 条 / 存量失败 {s['stable_fail']} 条")
            if s["rule_attribution"]:
                attr = "、".join(f"{k} × {v}" for k, v in s["rule_attribution"].items())
                lines.append(f"- **退化归因**：{attr}")
            if s["invariant_attribution"]:
                attr = "、".join(f"{k} × {v}" for k, v in s["invariant_attribution"].items())
                lines.append(f"- **触发的不变式**：{attr}")
            lines.append("")
            for d in rep.regressions:
                lines.append(f"- 🔴 **{d.case_id}** {d.title}（规则 {'、'.join(d.rules) or '未标注'}）")
                for f in d.new_failures:
                    lines.append(f"  - `{f}`")
            lines.append("")

    # ---------- 审计 ----------
    if audit_checks:
        lines.append("## 五、审计链完整性")
        lines.append("")
        lines.append("| 审计流 | 记录数 | 校验结果 |")
        lines.append("|---|---:|---|")
        for c in audit_checks:
            ok = "✅ 完整" if c.get("ok") else "❌ 断裂"
            lines.append(f"| {c.get('name', '')} | {c.get('records', 0)} | {ok} |")
        lines.append("")

    return "\n".join(lines)


def save_markdown(content: str, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p
