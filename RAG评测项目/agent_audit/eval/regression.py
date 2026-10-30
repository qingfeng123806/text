"""回归对比：把两次运行摆在一起，回答"这次改动让什么变坏了"。

评测只告诉你"现在有多少条挂了"，回归对比才告诉你
"这 3 条是这次改动新挂的，那 2 条是一直挂着的，还有 1 条被修好了"。

后者才是 CI 门禁需要的信号：一直挂着的可以排期，新挂的必须拦住。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

from .runner import CaseResult, RunResult


class DiffStatus(str, Enum):
    REGRESSION = "REGRESSION"      # 基线通过，本次失败 —— 新引入的退化
    FIXED = "FIXED"                # 基线失败，本次通过 —— 修好了
    STABLE_PASS = "STABLE_PASS"    # 一直通过
    STABLE_FAIL = "STABLE_FAIL"    # 一直失败（存量问题，非本次引入）
    NEW_CASE = "NEW_CASE"          # 基线里没有这个用例


@dataclass
class CaseDiff:
    case_id: str
    title: str
    rules: list[str]
    status: DiffStatus
    baseline_passed: Optional[bool]
    candidate_passed: bool
    new_failures: list[str] = field(default_factory=list)
    resolved_failures: list[str] = field(default_factory=list)
    persistent_failures: list[str] = field(default_factory=list)

    @property
    def is_regression(self) -> bool:
        return self.status is DiffStatus.REGRESSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "title": self.title,
            "rules": self.rules, "status": self.status.value,
            "baseline_passed": self.baseline_passed,
            "candidate_passed": self.candidate_passed,
            "new_failures": self.new_failures,
            "resolved_failures": self.resolved_failures,
            "persistent_failures": self.persistent_failures,
        }


@dataclass
class RegressionReport:
    baseline_run_id: str
    candidate_run_id: str
    baseline_agent: str
    candidate_agent: str
    created_at: str
    diffs: list[CaseDiff]

    # ---------- 汇总 ----------

    @property
    def regressions(self) -> list[CaseDiff]:
        return [d for d in self.diffs if d.status is DiffStatus.REGRESSION]

    @property
    def fixed(self) -> list[CaseDiff]:
        return [d for d in self.diffs if d.status is DiffStatus.FIXED]

    @property
    def stable_fail(self) -> list[CaseDiff]:
        return [d for d in self.diffs if d.status is DiffStatus.STABLE_FAIL]

    @property
    def has_regression(self) -> bool:
        return len(self.regressions) > 0

    def rule_attribution(self) -> dict[str, int]:
        """退化的用例分别归属哪条业务规则 —— 直接指向要改的代码。"""
        attr: dict[str, int] = {}
        for d in self.regressions:
            for rid in (d.rules or ["未标注"]):
                attr[rid] = attr.get(rid, 0) + 1
        return dict(sorted(attr.items()))

    def invariant_attribution(self) -> dict[str, int]:
        attr: dict[str, int] = {}
        for d in self.regressions:
            for f in d.new_failures:
                if f.startswith("INV-"):
                    inv = f.split(":")[0]
                    attr[inv] = attr.get(inv, 0) + 1
        return dict(sorted(attr.items()))

    def summary(self) -> dict[str, Any]:
        return {
            "baseline_run_id": self.baseline_run_id,
            "candidate_run_id": self.candidate_run_id,
            "baseline_agent": self.baseline_agent,
            "candidate_agent": self.candidate_agent,
            "created_at": self.created_at,
            "total_cases": len(self.diffs),
            "regressions": len(self.regressions),
            "fixed": len(self.fixed),
            "stable_pass": len([d for d in self.diffs
                                if d.status is DiffStatus.STABLE_PASS]),
            "stable_fail": len(self.stable_fail),
            "new_cases": len([d for d in self.diffs
                              if d.status is DiffStatus.NEW_CASE]),
            "has_regression": self.has_regression,
            "rule_attribution": self.rule_attribution(),
            "invariant_attribution": self.invariant_attribution(),
            "baseline_pass_rate": self._rate(
                [d.baseline_passed for d in self.diffs]),
            "candidate_pass_rate": self._rate(
                [d.candidate_passed for d in self.diffs]),
        }

    @staticmethod
    def _rate(flags: list[Optional[bool]]) -> float:
        real = [f for f in flags if f is not None]
        if not real:
            return 0.0
        return round(sum(1 for f in real if f) / len(real) * 100, 2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary(),
            "diffs": [d.to_dict() for d in self.diffs],
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p


def _as_run(src: Union[RunResult, dict[str, Any], str, Path]) -> RunResult:
    if isinstance(src, RunResult):
        return src
    if isinstance(src, dict):
        return RunResult.from_dict(src)
    p = Path(src)
    return RunResult.from_dict(json.loads(p.read_text(encoding="utf-8")))


def compare_runs(
    baseline: Union[RunResult, dict[str, Any], str, Path],
    candidate: Union[RunResult, dict[str, Any], str, Path],
) -> RegressionReport:
    b = _as_run(baseline)
    c = _as_run(candidate)

    diffs: list[CaseDiff] = []
    bmap = {cr.case_id: cr for cr in b.cases}

    for cr in c.cases:
        bcase = bmap.get(cr.case_id)
        b_fails = list(bcase.failed_checks) if bcase else []
        c_fails = list(cr.failed_checks)

        new_failures = [f for f in c_fails if f not in b_fails]
        resolved = [f for f in b_fails if f not in c_fails]
        persistent = [f for f in c_fails if f in b_fails]

        if bcase is None:
            status = DiffStatus.NEW_CASE
        elif bcase.passed and not cr.passed:
            status = DiffStatus.REGRESSION
        elif (not bcase.passed) and cr.passed:
            status = DiffStatus.FIXED
        elif cr.passed:
            status = DiffStatus.STABLE_PASS
        else:
            status = DiffStatus.STABLE_FAIL

        diffs.append(CaseDiff(
            case_id=cr.case_id, title=cr.title, rules=cr.rules,
            status=status,
            baseline_passed=(bcase.passed if bcase else None),
            candidate_passed=cr.passed,
            new_failures=new_failures,
            resolved_failures=resolved,
            persistent_failures=persistent,
        ))

    return RegressionReport(
        baseline_run_id=b.run_id,
        candidate_run_id=c.run_id,
        baseline_agent=b.agent_name or b.agent_id,
        candidate_agent=c.agent_name or c.agent_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        diffs=diffs,
    )


def compare_agents_in_run(run_dir: str | Path, baseline_agent: str,
                          candidate_agent: str) -> RegressionReport:
    """同一次运行内，两个 Agent 的结果对比。"""
    d = Path(run_dir) / "results"
    b = _as_run(d / f"{baseline_agent}.json")
    c = _as_run(d / f"{candidate_agent}.json")
    return compare_runs(b, c)
