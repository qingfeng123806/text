"""评测执行器：跑批、判分、落盘。

一次 Run = 一个 Agent × 全部用例。跑批产物全部落在 runs/<run_id>/ 下：
审计流、结构化 trace、结果摘要。回归对比读的就是这些产物，
不需要重跑基线。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..agent.protocol import Agent
from ..domain.order_store import OrderStore
from ..runtime.recorder import AuditLog
from ..runtime.session import run_session
from ..runtime.trace import Trace
from .assertions import AssertionResult, run_expectations
from .invariants import InvariantResult, has_blocking_violation, run_invariants
from .schema import Case


@dataclass
class CaseResult:
    case_id: str
    title: str
    rules: list[str]
    tags: list[str]
    agent_id: str
    trace_id: str
    passed: bool
    assertions: list[AssertionResult] = field(default_factory=list)
    invariants: list[InvariantResult] = field(default_factory=list)
    duration_ms: int = 0
    severity: str = "normal"
    # 从磁盘恢复时的缓存（内存态下由上面两个列表实时算出）
    failed_checks_data: list[str] = field(default_factory=list)
    critical_data: Optional[int] = None

    @property
    def failed_assertions(self) -> list[AssertionResult]:
        return [a for a in self.assertions if not a.passed]

    @property
    def violated_invariants(self) -> list[InvariantResult]:
        return [i for i in self.invariants if i.violated]

    @property
    def failed_checks(self) -> list[str]:
        """回归对比用的扁平指纹：失败项的唯一标识。"""
        if self.failed_checks_data:
            return self.failed_checks_data
        out = [f"{a.kind}: {a.message}" for a in self.failed_assertions]
        out += [f"{i.inv_id}({i.severity.value}): {i.message}"
                for i in self.violated_invariants]
        return out

    @property
    def critical_count(self) -> int:
        if self.critical_data is not None:
            return self.critical_data
        return sum(1 for i in self.violated_invariants
                   if i.severity.value == "CRITICAL")

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "title": self.title,
            "rules": self.rules, "tags": self.tags,
            "agent_id": self.agent_id, "trace_id": self.trace_id,
            "passed": self.passed, "duration_ms": self.duration_ms,
            "severity": self.severity,
            "assertions": [a.to_dict() for a in self.assertions],
            "invariants": [i.to_dict() for i in self.invariants],
            "failed_checks": self.failed_checks,
            "critical_count": self.critical_count,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CaseResult":
        return cls(
            case_id=d["case_id"], title=d.get("title", ""),
            rules=list(d.get("rules", [])), tags=list(d.get("tags", [])),
            agent_id=d.get("agent_id", ""), trace_id=d.get("trace_id", ""),
            passed=bool(d.get("passed", False)),
            duration_ms=int(d.get("duration_ms", 0)),
            severity=d.get("severity", "normal"),
            # 恢复时只保留可比对的信息，不做二次计算
            failed_checks_data=list(d.get("failed_checks", [])),
            critical_data=d.get("critical_count"),
        )


@dataclass
class RunResult:
    run_id: str
    agent_id: str
    agent_name: str
    created_at: str
    cases: list[CaseResult]
    traces: list[dict[str, Any]] = field(default_factory=list)
    config: dict[str, Any] = field(default_factory=dict)
    audit_path: str = ""

    # ---------- 汇总 ----------

    @property
    def total(self) -> int:
        return len(self.cases)

    @property
    def passed(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0

    @property
    def critical_count(self) -> int:
        return sum(c.critical_count for c in self.cases)

    def summary(self) -> dict[str, Any]:
        by_rule: dict[str, dict[str, int]] = {}
        for c in self.cases:
            for rid in (c.rules or ["-"]):
                st = by_rule.setdefault(rid, {"total": 0, "passed": 0, "failed": 0})
                st["total"] += 1
                st["passed" if c.passed else "failed"] += 1

        inv_hits: dict[str, int] = {}
        for c in self.cases:
            for i in c.violated_invariants:
                inv_hits[i.inv_id] = inv_hits.get(i.inv_id, 0) + 1

        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "created_at": self.created_at,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": round(self.pass_rate, 2),
            "critical_violations": self.critical_count,
            "by_rule": by_rule,
            "invariant_violations": inv_hits,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id, "agent_id": self.agent_id,
            "agent_name": self.agent_name, "created_at": self.created_at,
            "config": self.config, "audit_path": self.audit_path,
            "summary": self.summary(),
            "cases": [c.to_dict() for c in self.cases],
            "traces": self.traces,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunResult":
        """从磁盘恢复。用于回归对比时读取历史基线，无需重跑。"""
        return cls(
            run_id=d["run_id"], agent_id=d.get("agent_id", ""),
            agent_name=d.get("agent_name", ""),
            created_at=d.get("created_at", ""),
            cases=[CaseResult.from_dict(c) for c in d.get("cases", [])],
            traces=list(d.get("traces", [])),
            config=dict(d.get("config", {})),
            audit_path=d.get("audit_path", ""),
        )

    def get_case(self, case_id: str) -> Optional[CaseResult]:
        for c in self.cases:
            if c.case_id == case_id:
                return c
        return None

    @property
    def trace_map(self) -> dict[str, dict[str, Any]]:
        return {t.get("case_id", ""): t for t in self.traces}

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return p


class EvalRunner:
    def __init__(
        self,
        cases: list[Case],
        store: OrderStore,
        run_id: str,
        out_dir: str | Path = "runs",
        clock_mode: str = "virtual",
        audit_enabled: bool = True,
    ):
        self.cases = cases
        self.store = store
        self.run_id = run_id
        self.out_dir = Path(out_dir)
        self.clock_mode = clock_mode
        self.audit_enabled = audit_enabled

    def run_agent(self, agent: Agent) -> RunResult:
        started = time.perf_counter()
        run_dir = self.out_dir / self.run_id
        results_dir = run_dir / "results"
        audit_dir = run_dir / "audit"
        results_dir.mkdir(parents=True, exist_ok=True)
        audit_dir.mkdir(parents=True, exist_ok=True)

        audit_path = audit_dir / f"{agent.agent_id}.jsonl"
        case_results: list[CaseResult] = []
        trace_dicts: list[dict[str, Any]] = []

        with AuditLog(audit_path, enabled=self.audit_enabled) as audit:
            for case in self.cases:
                cr, tdict = self._run_case(agent, case, audit)
                case_results.append(cr)
                trace_dicts.append(tdict)

        elapsed = int((time.perf_counter() - started) * 1000)
        rr = RunResult(
            run_id=self.run_id,
            agent_id=agent.agent_id,
            agent_name=agent.display_name,
            created_at=datetime.now().isoformat(timespec="seconds"),
            cases=case_results,
            traces=trace_dicts,
            config={
                "clock_mode": self.clock_mode,
                "case_count": len(self.cases),
                "wall_clock_ms": elapsed,
                "agent": agent.describe(),
            },
            audit_path=str(audit_path).replace("\\", "/"),
        )
        rr.save(results_dir / f"{agent.agent_id}.json")
        return rr

    def _run_case(self, agent: Agent, case: Case,
                  audit: AuditLog) -> tuple[CaseResult, dict[str, Any]]:
        t0 = time.perf_counter()
        trace, _store, _ledger = run_session(
            agent=agent,
            store=self.store.clone(),
            messages=case.messages,
            user_id=case.user_id,
            case_id=case.case_id,
            case_title=case.title,
            run_id=self.run_id,
            faults=case.faults,
            audit=audit,
            clock_mode=self.clock_mode,
            deadline_ms=case.deadline_ms,
            tags=case.tags + case.rules,
        )
        assertions = run_expectations(trace, case.expect)
        invariants = run_invariants(trace)

        assertions_ok = all(a.passed for a in assertions)
        blocking = has_blocking_violation(invariants)
        passed = assertions_ok and not blocking

        cr = CaseResult(
            case_id=case.case_id, title=case.title, rules=case.rules,
            tags=case.tags, agent_id=agent.agent_id, trace_id=trace.trace_id,
            passed=passed, assertions=assertions, invariants=invariants,
            duration_ms=int((time.perf_counter() - t0) * 1000),
            severity=case.severity,
        )
        return cr, trace.to_dict()

    def write_manifest(self, runs: list[RunResult]) -> dict[str, Any]:
        manifest = {
            "run_id": self.run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "clock_mode": self.clock_mode,
            "case_count": len(self.cases),
            "agents": [{"agent_id": r.agent_id, "agent_name": r.agent_name,
                        "pass_rate": r.pass_rate, "passed": r.passed,
                        "total": r.total,
                        "critical": r.critical_count} for r in runs],
        }
        p = self.out_dir / self.run_id / "manifest.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                     encoding="utf-8")
        return manifest


def load_run(run_id: str, agent_id: str, out_dir: str | Path = "runs") -> dict[str, Any]:
    p = Path(out_dir) / run_id / "results" / f"{agent_id}.json"
    if not p.exists():
        raise FileNotFoundError(f"找不到运行结果: {p}")
    return json.loads(p.read_text(encoding="utf-8"))
