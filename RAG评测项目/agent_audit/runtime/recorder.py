"""审计日志：追加式 JSONL + SHA-256 哈希链。

为什么要哈希链 —— 审计记录如果可以被悄悄改写，那它就不是证据，只是日记。
每条事件带上前一条的哈希，任何一次插入、删除或篡改都会让链条断裂，
`verify` 会精确指出断在第几条。

这是"审计"和"日志"的分界线。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _digest(prev_hash: str, body: str) -> str:
    return hashlib.sha256(f"{prev_hash}|{body}".encode("utf-8")).hexdigest()


class AuditLog:
    """追加式审计日志。只写，不改，不删。"""

    def __init__(self, path: str | Path, enabled: bool = True,
                 rotate_existing: bool = True):
        """打开审计日志。

        rotate_existing=True（默认）：文件已存在时先把旧链归档到 archive/，
        本次运行从创世哈希重新开始。这样"一条链 = 一次运行"，链长就是这次
        运行的事件数——否则同一 run_id 反复重跑会让事件静默累积，
        审计记录数再也对不上任何一次真实运行。

        rotate_existing=False：显式续写，从末尾恢复序号与哈希。
        只在确实要把多次运行串成同一条链时使用。
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self.rotate_existing = rotate_existing
        self.rotated_to: Optional[Path] = None
        self._seq = 0
        self._prev = GENESIS
        self._buffer: list[dict[str, Any]] = []
        if self.enabled:
            if rotate_existing:
                self.rotated_to = self._rotate()
            self._resume()
            self._fh = open(self.path, "a", encoding="utf-8")
        else:
            self._fh = None

    def _rotate(self) -> Optional[Path]:
        """把上一次运行的审计链挪到 archive/ 下，返回归档路径。"""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return None
        archive_dir = self.path.parent / "archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = archive_dir / f"{self.path.stem}.{stamp}.jsonl"
        n = 1
        while target.exists():
            target = archive_dir / f"{self.path.stem}.{stamp}-{n}.jsonl"
            n += 1
        self.path.replace(target)
        return target

    def _resume(self) -> None:
        """从已有日志末尾恢复序号与前值哈希。"""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        last: Optional[dict[str, Any]] = None
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        last = json.loads(line)
        except (OSError, json.JSONDecodeError):
            return
        if last:
            self._seq = int(last.get("seq", 0))
            self._prev = str(last.get("hash", GENESIS))

    # ---------- 写入 ----------

    def append(self, event_type: str, payload: dict[str, Any],
               trace_id: str = "") -> Optional[dict[str, Any]]:
        if not self.enabled:
            return None
        self._seq += 1
        body_obj = {
            "seq": self._seq,
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "type": event_type,
            "trace_id": trace_id,
            "payload": payload,
        }
        h = _digest(self._prev, _canonical(body_obj))
        rec = dict(body_obj)
        rec["prev_hash"] = self._prev
        rec["hash"] = h
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()
        self._prev = h
        self._buffer.append(rec)
        return rec

    # ---------- 读取与校验 ----------

    @staticmethod
    def read(path: str | Path) -> list[dict[str, Any]]:
        p = Path(path)
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                out.append(json.loads(line))
        return out

    @staticmethod
    def verify(path: str | Path) -> dict[str, Any]:
        """逐条重算哈希，返回校验结论。"""
        records = AuditLog.read(path)
        if not records:
            return {"ok": True, "records": 0, "message": "空日志（无内容可校验）",
                    "broken_at": None}

        prev = GENESIS
        for i, rec in enumerate(records, start=1):
            expected_prev = rec.get("prev_hash", "")
            if expected_prev != prev:
                return {
                    "ok": False, "records": len(records), "broken_at": i,
                    "message": (f"第 {i} 条记录的前向哈希断裂："
                                f"期望 {prev[:12]}…，实际 {expected_prev[:12]}…"
                                f"（记录可能被插入或删除）"),
                }
            body = {k: rec[k] for k in ("seq", "ts", "type", "trace_id", "payload")
                    if k in rec}
            recomputed = _digest(prev, _canonical(body))
            if recomputed != rec.get("hash"):
                return {
                    "ok": False, "records": len(records), "broken_at": i,
                    "message": (f"第 {i} 条记录内容被篡改："
                                f"重算哈希 {recomputed[:12]}… 与记录值 "
                                f"{str(rec.get('hash'))[:12]}… 不一致"),
                }
            prev = rec["hash"]

        return {
            "ok": True, "records": len(records), "broken_at": None,
            "head_hash": prev,
            "message": f"哈希链完整，{len(records)} 条记录全部通过校验",
        }

    # ---------- 生命周期 ----------

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
