"""本地 Web 控制台后端。

用标准库 http.server 起服务，不引入任何 Web 框架：
这个系统要能在一台没有外网的机器上运行，装依赖本身就是一种风险。
"""

from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from ..eval.invariants import INVARIANTS
from ..eval.regression import compare_runs
from ..eval.runner import RunResult
from ..eval.schema import load_cases
from ..runtime.recorder import AuditLog

BASELINE_AGENT = "refund-agent-v1"


class ConsoleState:
    def __init__(self, runs_dir: Path, root: Path,
                 cases_path: Optional[Path] = None):
        self.runs_dir = Path(runs_dir)
        self.root = Path(root)
        self.cases_path = Path(cases_path) if cases_path else \
            self.root / "cases" / "refund_cases.json"
        self._cache: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---------- 运行列表 ----------

    def list_runs(self) -> list[dict[str, Any]]:
        out = []
        if not self.runs_dir.exists():
            return out
        for d in sorted(self.runs_dir.iterdir(),
                        key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir() or not (d / "results").exists():
                continue
            mf = d / "manifest.json"
            agents = []
            if mf.exists():
                try:
                    agents = json.loads(mf.read_text(encoding="utf-8")).get("agents", [])
                except json.JSONDecodeError:
                    agents = []
            else:
                for f in sorted((d / "results").glob("*.json")):
                    agents.append({"agent_id": f.stem, "agent_name": f.stem})
            out.append({
                "run_id": d.name,
                "mtime": int(d.stat().st_mtime),
                "agents": agents,
                "has_audit": (d / "audit").exists(),
                "has_report": (d / "report.html").exists(),
            })
        return out

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir / run_id

    # ---------- 结果 ----------

    def invalidate(self) -> int:
        """清空结果缓存。用户在别处重跑了评测后点「刷新」，必须能看到新数据。"""
        with self._lock:
            n = len(self._cache)
            self._cache.clear()
        return n

    def load_result(self, run_id: str, agent_id: str) -> dict[str, Any]:
        key = f"{run_id}/{agent_id}"
        with self._lock:
            cached = self._cache.get(key)
        if cached is not None:
            return cached
        p = self.run_dir(run_id) / "results" / f"{agent_id}.json"
        if not p.exists():
            raise FileNotFoundError(f"没有 {run_id} 下 {agent_id} 的结果：{p}")
        data = json.loads(p.read_text(encoding="utf-8"))
        with self._lock:
            self._cache[key] = data
        return data

    def list_agents(self, run_id: str) -> list[str]:
        d = self.run_dir(run_id) / "results"
        if not d.exists():
            return []
        return sorted(f.stem for f in d.glob("*.json"))

    # ---------- 派生视图 ----------

    def overview(self, run_id: str) -> dict[str, Any]:
        agents = self.list_agents(run_id)
        rows = []
        for aid in agents:
            data = self.load_result(run_id, aid)
            s = data.get("summary", {})
            rows.append(s)
        # 基线排在最前
        rows.sort(key=lambda s: (s.get("agent_id") != BASELINE_AGENT,
                                 s.get("agent_id", "")))
        return {
            "run_id": run_id,
            "runs": self.list_runs(),
            "agents": rows,
            "case_count": rows[0].get("total", 0) if rows else 0,
        }

    def regression(self, run_id: str,
                   candidate: Optional[str] = None) -> dict[str, Any]:
        agents = self.list_agents(run_id)
        base = None
        if BASELINE_AGENT in agents:
            base = RunResult.from_dict(self.load_result(run_id, BASELINE_AGENT))
        targets = [candidate] if candidate else \
            [a for a in agents if a != BASELINE_AGENT]
        reports = []
        for aid in targets:
            if aid not in agents:
                continue
            if base is None:
                continue
            cand = RunResult.from_dict(self.load_result(run_id, aid))
            rep = compare_runs(base, cand)
            reports.append(rep.to_dict())
        return {"run_id": run_id, "baseline": BASELINE_AGENT, "reports": reports}

    def audit_verify(self, run_id: str) -> dict[str, Any]:
        d = self.run_dir(run_id) / "audit"
        out = []
        if d.exists():
            for f in sorted(d.glob("*.jsonl")):
                v = AuditLog.verify(f)
                v["name"] = f.stem
                v["path"] = str(f.relative_to(self.root)).replace("\\", "/")
                out.append(v)
        return {"run_id": run_id, "checks": out}

    def trace(self, run_id: str, agent_id: str, case_id: str) -> dict[str, Any]:
        data = self.load_result(run_id, agent_id)
        for t in data.get("traces", []):
            if t.get("case_id") == case_id:
                return t
        raise FileNotFoundError(f"找不到 trace: {run_id}/{agent_id}/{case_id}")

    def invariants(self) -> list[dict[str, Any]]:
        return [{
            "inv_id": inv.inv_id, "name": inv.name,
            "severity": inv.severity.value, "rule_id": inv.rule_id,
            "statement": inv.statement,
        } for inv in INVARIANTS]

    def cases(self) -> list[dict[str, Any]]:
        if not self.cases_path.exists():
            return []
        return [c.to_dict() for c in load_cases(self.cases_path)]

    def health(self) -> dict[str, Any]:
        """探活。前端启动时第一个打的接口，用来判断服务在不在、产物有没有。"""
        runs = self.list_runs()
        try:
            case_count = len(self.cases())
        except Exception:  # noqa: BLE001
            case_count = 0
        return {
            "ok": True,
            "service": "agent-audit-console",
            "version": "1.0",
            "runs_dir": str(self.runs_dir),
            "case_file": str(self.cases_path),
            "case_count": case_count,
            "run_count": len(runs),
            "latest_run": runs[0]["run_id"] if runs else "",
            "server_time": datetime.now().isoformat(timespec="seconds"),
        }


# ============================================================
# HTTP
# ============================================================

class Handler(BaseHTTPRequestHandler):
    state: ConsoleState = None  # type: ignore[assignment]
    web_dir: Path = Path("web")
    server_version = "AgentAudit/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        # 静默默认访问日志，避免刷屏；错误仍会走 log_error
        pass

    # ---------- 响应 ----------

    def _cors(self) -> None:
        """允许跨域。

        服务只绑在 127.0.0.1，且页面本身没有敏感态。放开 CORS 是为了支持一种
        很常见的用法：用户直接双击打开 web/index.html（file:// 协议），
        此时 origin 是 null，不放开就连不上本地服务，页面会误报「初始化失败」。
        """
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Accept")

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str) -> None:
        if not path.exists():
            self.send_error(404, f"not found: {path.name}")
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path: Path) -> None:
        self._file(path, "text/html; charset=utf-8")

    # ---------- 路由 ----------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [unquote(p) for p in parsed.path.strip("/").split("/") if p]

        try:
            if not parts or parts == ["index.html"]:
                self._html(self.web_dir / "index.html")
                return
            if parts[0] != "api":
                # 静态资源
                p = self.web_dir.joinpath(*parts)
                if p.exists() and p.is_file():
                    ctype = "text/plain; charset=utf-8"
                    if p.suffix == ".css":
                        ctype = "text/css; charset=utf-8"
                    elif p.suffix == ".js":
                        ctype = "application/javascript; charset=utf-8"
                    self._file(p, ctype)
                else:
                    self._html(self.web_dir / "index.html")
                return

            self._api(parts[1:])
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # 预检请求：file:// 页面带 Origin: null 时会先发一次 OPTIONS
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def _api(self, segs: list[str]) -> None:
        st = self.state
        if not segs:
            self._json({"ok": True, "service": "agent-audit-console"})
            return

        if segs[0] == "health":
            self._json(st.health())
            return

        if segs[0] == "refresh":
            n = st.invalidate()
            self._json({"ok": True, "cleared": n, "runs": len(st.list_runs())})
            return

        if segs[0] == "runs":
            self._json({"runs": st.list_runs()})
            return

        if segs[0] == "overview" and len(segs) >= 2:
            self._json(st.overview(segs[1]))
            return

        if segs[0] == "result" and len(segs) >= 3:
            self._json(st.load_result(segs[1], segs[2]))
            return

        if segs[0] == "regression" and len(segs) >= 2:
            cand = segs[2] if len(segs) >= 3 else None
            self._json(st.regression(segs[1], cand))
            return

        if segs[0] == "audit" and len(segs) >= 2:
            self._json(st.audit_verify(segs[1]))
            return

        if segs[0] == "trace" and len(segs) >= 4:
            self._json(st.trace(segs[1], segs[2], segs[3]))
            return

        if segs[0] == "invariants":
            self._json({"invariants": st.invariants()})
            return

        if segs[0] == "cases":
            self._json({"cases": st.cases()})
            return

        if segs[0] == "report" and len(segs) >= 2:
            p = st.run_dir(segs[1]) / "report.html"
            self._html(p)
            return

        self._json({"error": f"未知接口 /api/{'/'.join(segs)}"}, 404)


def run_server(port: int = 8770, runs_dir: str | Path = "runs",
               root: str | Path = ".", open_browser: bool = True) -> int:
    root_p = Path(root).resolve()
    state = ConsoleState(Path(runs_dir), root_p)

    handler = type("BoundHandler", (Handler,), {
        "state": state,
        "web_dir": root_p / "web",
    })

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError as exc:
        print(f"端口 {port} 被占用或无法绑定：{exc}")
        return 1

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Agent 行为审计控制台已启动")
    print(f"  地址：{url}")
    print(f"  运行目录：{Path(runs_dir).resolve()}")
    print(f"\n  按 Ctrl+C 停止\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭控制台…")
    finally:
        httpd.server_close()
    return 0
