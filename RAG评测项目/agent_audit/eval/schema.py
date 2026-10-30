"""评测用例的定义与加载。

用例用 JSON 描述，方便评审和版本管理：
业务同学能读懂，Code Review 时 diff 出来的是"哪条预期变了"，而不是散落的代码。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class Case:
    case_id: str
    title: str
    description: str = ""
    rules: list[str] = field(default_factory=list)   # 覆盖的业务规则 R1-R4
    tags: list[str] = field(default_factory=list)
    user_id: str = "u_alice"
    messages: list[str] = field(default_factory=list)
    faults: list[dict[str, Any]] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)
    deadline_ms: int = 1000
    severity: str = "normal"      # normal | critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id, "title": self.title,
            "description": self.description, "rules": self.rules,
            "tags": self.tags, "user_id": self.user_id,
            "messages": self.messages, "faults": self.faults,
            "expect": self.expect, "deadline_ms": self.deadline_ms,
            "severity": self.severity,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Case":
        return cls(
            case_id=d["case_id"],
            title=d.get("title", d["case_id"]),
            description=d.get("description", ""),
            rules=list(d.get("rules", [])),
            tags=list(d.get("tags", [])),
            user_id=d.get("user_id", "u_alice"),
            messages=list(d.get("messages", [])),
            faults=list(d.get("faults", [])),
            expect=dict(d.get("expect", {})),
            deadline_ms=int(d.get("deadline_ms", 1000)),
            severity=d.get("severity", "normal"),
        )


def load_cases(path: str | Path) -> list[Case]:
    p = Path(path)
    raw = json.loads(p.read_text(encoding="utf-8"))
    items = raw.get("cases", raw) if isinstance(raw, dict) else raw
    return [Case.from_dict(d) for d in items]


def load_case_dir(dirpath: str | Path) -> list[Case]:
    d = Path(dirpath)
    cases: list[Case] = []
    for f in sorted(d.glob("*.json")):
        cases.extend(load_cases(f))
    return cases


def save_cases(cases: list[Case], path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"cases": [c.to_dict() for c in cases]},
                            ensure_ascii=False, indent=2), encoding="utf-8")
