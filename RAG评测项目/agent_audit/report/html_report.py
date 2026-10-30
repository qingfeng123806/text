"""HTML 报告：单文件、零依赖、可直接双击打开或归档。

刻意不用任何 CDN 和前端框架 —— 评测报告是要能归档三年后还打得开的东西，
链到外部资源的报告，将来打开就是一片白。
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from ..eval.regression import RegressionReport
from ..eval.runner import RunResult


def esc(s: Any) -> str:
    return html.escape(str(s), quote=True)


_CSS = """
:root{
  --bg:#f6f7f9; --panel:#ffffff; --ink:#1b1f24; --muted:#6b7280;
  --line:#e5e7eb; --brand:#2f6feb; --ok:#15803d; --warn:#b45309;
  --bad:#dc2626; --crit:#991b1b; --chip:#eef2ff;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:14px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:28px 22px 64px}
header{background:linear-gradient(135deg,#1e3a8a,#2f6feb);color:#fff;
  border-radius:14px;padding:26px 28px;margin-bottom:24px;
  box-shadow:0 8px 24px rgba(30,58,138,.18)}
header h1{margin:0 0 6px;font-size:23px;letter-spacing:.3px}
header .sub{opacity:.85;font-size:13px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin:22px 0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card .k{color:var(--muted);font-size:12px;letter-spacing:.4px}
.card .v{font-size:26px;font-weight:650;margin-top:4px}
.card .v.ok{color:var(--ok)} .card .v.bad{color:var(--bad)} .card .v.warn{color:var(--warn)}
section{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:20px 22px;margin-bottom:20px}
section h2{margin:0 0 4px;font-size:17px}
section .hint{color:var(--muted);font-size:12.5px;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:9px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
th{background:#fafbfc;color:var(--muted);font-weight:600;font-size:12px;letter-spacing:.3px}
tr:last-child td{border-bottom:none}
.bar{display:inline-block;width:150px;height:8px;background:#eef0f3;border-radius:5px;overflow:hidden;vertical-align:middle}
.bar i{display:block;height:100%;background:var(--ok);border-radius:5px}
.bar i.warn{background:var(--warn)} .bar i.bad{background:var(--bad)}
.tag{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;
  background:var(--chip);color:#3730a3;margin-right:4px}
.tag.r{background:#fee2e2;color:var(--crit)} .tag.g{background:#dcfce7;color:var(--ok)}
.tag.o{background:#ffedd5;color:var(--warn)} .tag.b{background:#dbeafe;color:#1e40af}
.pill{display:inline-block;padding:2px 9px;border-radius:6px;font-size:12px;font-weight:600}
.pill.ok{background:#dcfce7;color:var(--ok)} .pill.bad{background:#fee2e2;color:var(--bad)}
details{border:1px solid var(--line);border-radius:9px;margin:9px 0;background:#fff}
details[open]{background:#fff}
summary{cursor:pointer;padding:11px 14px;font-size:13.5px;font-weight:600;list-style:none;
  display:flex;align-items:center;gap:9px;flex-wrap:wrap}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸";color:var(--muted);transition:transform .15s}
details[open] summary::before{transform:rotate(90deg)}
.body{padding:4px 16px 14px;border-top:1px solid var(--line)}
.fail{color:var(--bad)} .crit{color:var(--crit);font-weight:600}
.muted{color:var(--muted)}
ul.chk{margin:8px 0 0;padding-left:20px}
ul.chk li{margin:4px 0;font-size:12.8px;font-family:ui-monospace,Menlo,Consolas,monospace}
.tl{margin:6px 0 0;padding:0;list-style:none}
.tl li{position:relative;padding:9px 0 9px 26px;border-left:2px solid var(--line);margin-left:6px}
.tl li::before{content:"";position:absolute;left:-6px;top:15px;width:10px;height:10px;
  border-radius:50%;background:var(--brand);border:2px solid #fff}
.tl li.user::before{background:#64748b}
.tl li.tool::before{background:#0891b2}
.tl li.rule::before{background:#7c3aed}
.tl .who{font-size:11.5px;color:var(--muted);letter-spacing:.3px}
.tl .txt{font-size:13px;margin:2px 0}
.tl .meta{font-size:11.5px;color:var(--muted)}
code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:12.2px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:26px}
"""


def _rate_cls(rate: float) -> str:
    if rate >= 99.9:
        return "ok"
    return "warn" if rate >= 80 else "bad"


def _case_block(cr: dict[str, Any], trace: Optional[dict[str, Any]] = None) -> str:
    rules = "".join(f'<span class="tag b">{esc(r)}</span>' for r in cr.get("rules", []))
    pill = ('<span class="pill ok">PASS</span>' if cr.get("passed")
            else '<span class="pill bad">FAIL</span>')
    crit = cr.get("critical_count", 0)
    crit_tag = f'<span class="tag r">CRITICAL × {crit}</span>' if crit else ""

    body: list[str] = []
    if not cr.get("passed"):
        body.append('<div style="margin-top:10px"><b class="fail">失败项</b>'
                    '<ul class="chk">')
        for f in cr.get("failed_checks", []):
            cls = 'class="crit"' if f.startswith("INV-") else ""
            body.append(f'<li {cls}>{esc(f)}</li>')
        body.append("</ul></div>")

    if trace:
        body.append('<div style="margin-top:12px"><b>行为轨迹回放</b>'
                    '<ul class="tl">')
        for t in trace.get("turns", []):
            body.append(f'<li class="user"><span class="who">第 {t["index"]} 轮 · 用户</span>'
                        f'<div class="txt">{esc(t["user_message"])}</div></li>')
            reply = t.get("reply") or {}
            for rd in reply.get("rule_decisions", []):
                mark = "命中" if rd.get("matched") else "未命中"
                cls = "crit" if (rd.get("matched") and "DISABLED" in (rd.get("reason_code") or "")
                                 or "SKIPPED" in (rd.get("reason_code") or "")) else ""
                body.append(
                    f'<li class="rule"><span class="who">规则判定 · {esc(rd["rule_id"])}</span>'
                    f'<div class="txt"><span class="tag">{mark}</span> '
                    f'<code>{esc(rd.get("reason_code",""))}</code> {esc(rd.get("message",""))}</div></li>')
            for tc in reply.get("tool_calls", []):
                moved = "💰 已动钱" if tc.get("money_moved") else "未动钱"
                st = tc.get("status")
                stcls = "fail" if st in ("TIMEOUT", "ERROR") else ""
                arg = tc.get("args", {}).get("order_id", "")
                body.append(
                    f'<li class="tool"><span class="who">工具调用 · {esc(tc["tool"])}</span>'
                    f'<div class="txt"><code>{esc(arg)}</code> '
                    f'<span class="{stcls}">{esc(st)}</span> · {moved} · {tc.get("duration_ms",0)} ms</div>'
                    + (f'<div class="meta">{esc(tc.get("error",""))}</div>'
                       if tc.get("error") else "") + "</li>")
            body.append(
                f'<li><span class="who">Agent 回复 · {esc(reply.get("action",""))}</span>'
                f'<div class="txt">{esc(reply.get("text",""))}</div></li>')
        body.append("</ul></div>")

    inner = "\n".join(body)
    return (f'<details><summary>{pill} <code>{esc(cr["case_id"])}</code> '
            f'{esc(cr.get("title",""))} {rules}{crit_tag}</summary>'
            f'<div class="body">{inner}</div></details>')


def render_html_report(
    runs: list[RunResult],
    regressions: Optional[list[RegressionReport]] = None,
    audit_checks: Optional[list[dict[str, Any]]] = None,
    out_path: str | Path = "runs/report.html",
    title: str = "Agent 行为审计与回归评测报告",
) -> Path:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total_cases = max((r.total for r in runs), default=0)
    best = max((r.pass_rate for r in runs), default=0.0)
    total_crit = sum(r.critical_count for r in runs)
    total_fail = sum(r.failed for r in runs)

    parts: list[str] = [
        "<!DOCTYPE html><html lang='zh-CN'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>{esc(title)}</title><style>{_CSS}</style></head><body><div class='wrap'>",
        f"<header><h1>{esc(title)}</h1>"
        f"<div class='sub'>生成时间 {esc(now)} · 共 {len(runs)} 个 Agent 版本 · "
        f"{total_cases} 条用例 · 零依赖离线报告</div></header>",
    ]

    # ---------- 概览卡片 ----------
    parts.append("<div class='cards'>")
    parts.append(f"<div class='card'><div class='k'>参评 Agent</div>"
                 f"<div class='v'>{len(runs)}</div></div>")
    parts.append(f"<div class='card'><div class='k'>用例总数</div>"
                 f"<div class='v'>{total_cases}</div></div>")
    parts.append(f"<div class='card'><div class='k'>最佳通过率</div>"
                 f"<div class='v {_rate_cls(best)}'>{best:.1f}%</div></div>")
    parts.append(f"<div class='card'><div class='k'>失败用例合计</div>"
                 f"<div class='v {'ok' if total_fail==0 else 'bad'}'>{total_fail}</div></div>")
    parts.append(f"<div class='card'><div class='k'>严重违规(CRITICAL)</div>"
                 f"<div class='v {'ok' if total_crit==0 else 'bad'}'>{total_crit}</div></div>")
    parts.append("</div>")

    # ---------- Agent 对比 ----------
    parts.append("<section><h2>一、Agent 版本横向对比</h2>"
                 "<div class='hint'>同一份用例集、同一套不变式，跑在不同 Agent 版本上。"
                 "通过率下降即代表该版本引入了行为退化。</div>")
    parts.append("<table><thead><tr><th>Agent 版本</th><th>通过 / 总数</th>"
                 "<th>通过率</th><th>严重违规</th><th>触发的不变式</th></tr></thead><tbody>")
    for r in runs:
        inv = r.summary().get("invariant_violations", {})
        inv_html = "".join(f"<span class='tag r'>{esc(k)}×{v}</span>" for k, v in sorted(inv.items())) \
            or "<span class='muted'>无</span>"
        parts.append(
            f"<tr><td><b>{esc(r.agent_name)}</b><br><span class='muted' style='font-size:12px'>"
            f"{esc(r.agent_id)}</span></td>"
            f"<td>{r.passed} / {r.total}</td>"
            f"<td><span class='bar'><i class='{_rate_cls(r.pass_rate)}' "
            f"style='width:{r.pass_rate:.1f}%'></i></span> {r.pass_rate:.1f}%</td>"
            f"<td>{'<b class=crit>%d</b>' % r.critical_count if r.critical_count else '0'}</td>"
            f"<td>{inv_html}</td></tr>")
    parts.append("</tbody></table></section>")

    # ---------- 规则维度 ----------
    parts.append("<section><h2>二、按业务规则维度的通过情况</h2>"
                 "<div class='hint'>R1 无单号追问 · R2 跨用户拒绝 · R3 单次退款 · R4 超时不重试</div>")
    parts.append("<table><thead><tr><th>规则</th><th>Agent</th><th>通过 / 总数</th>"
                 "<th>通过率</th></tr></thead><tbody>")
    rule_labels = {"R1": "无订单号先追问", "R2": "跨用户订单拒绝",
                   "R3": "同一订单只能退款一次", "R4": "工具超时不能重复退款"}
    for rid, label in rule_labels.items():
        for r in runs:
            st = r.summary()["by_rule"].get(rid)
            if not st:
                continue
            rate = st["passed"] / st["total"] * 100 if st["total"] else 0
            parts.append(
                f"<tr><td><span class='tag b'>{rid}</span>{esc(label)}</td>"
                f"<td>{esc(r.agent_id)}</td><td>{st['passed']} / {st['total']}</td>"
                f"<td><span class='bar'><i class='{_rate_cls(rate)}' style='width:{rate:.1f}%'></i></span>"
                f" {rate:.0f}%</td></tr>")
    parts.append("</tbody></table></section>")

    # ---------- 回归 ----------
    if regressions:
        parts.append("<section><h2>三、回归对比</h2>"
                     "<div class='hint'>只看通过率无法区分\"这次改坏的\"和\"本来就坏的\"。"
                     "回归对比把两者分开：新增失败必须拦截，存量失败可以排期。</div>")
        for rep in regressions:
            s = rep.summary()
            verdict = ('<span class="pill bad">存在回归，建议拦截</span>' if s["has_regression"]
                       else '<span class="pill ok">无新增回归</span>')
            parts.append(
                f"<div style='margin:14px 0 8px'><b>{esc(s['baseline_agent'])} → "
                f"{esc(s['candidate_agent'])}</b> {verdict}<br>"
                f"<span class='muted'>基线 {s['baseline_pass_rate']}% → 当前 "
                f"{s['candidate_pass_rate']}% ｜ 新增回归 {s['regressions']} · "
                f"修复 {s['fixed']} · 稳定通过 {s['stable_pass']} · 存量失败 {s['stable_fail']}</span></div>")
            if s["rule_attribution"]:
                parts.append("<div style='margin:6px 0'><b>退化归因：</b>" + "".join(
                    f"<span class='tag r'>{esc(k)} × {v}</span>"
                    for k, v in s["rule_attribution"].items()) + "</div>")
            if s["invariant_attribution"]:
                parts.append("<div style='margin:6px 0'><b>触发不变式：</b>" + "".join(
                    f"<span class='tag o'>{esc(k)}</span>"
                    for k in s["invariant_attribution"].items()) + "</div>")
            for d in rep.regressions:
                parts.append(
                    f"<details><summary><span class='pill bad'>REGRESSION</span> "
                    f"<code>{esc(d.case_id)}</code> {esc(d.title)} "
                    + "".join(f"<span class='tag b'>{esc(x)}</span>" for x in d.rules)
                    + "</summary><div class='body'><b class='fail'>本次新增的失败项</b>"
                    "<ul class='chk'>"
                    + "".join(f"<li>{esc(f)}</li>" for f in d.new_failures)
                    + "</ul></div></details>")
        parts.append("</section>")

    # ---------- 用例明细 ----------
    parts.append("<section><h2>四、失败用例的行为轨迹</h2>"
                 "<div class='hint'>每一条失败都附完整的决策回放：用户说了什么、命中了哪条规则、"
                 "调用了什么工具、是否真的动了钱。展开即可复盘。</div>")
    any_fail = False
    for r in runs:
        failures = [c for c in r.cases if not c.passed]
        if not failures:
            continue
        any_fail = True
        tmap = r.trace_map
        parts.append(f"<div style='margin:14px 0 6px'><b>{esc(r.agent_name)}</b> "
                     f"<span class='muted'>{r.passed}/{r.total} 通过</span></div>")
        for c in failures:
            parts.append(_case_block(c.to_dict(), tmap.get(c.case_id)))
    if not any_fail:
        parts.append("<p class='muted'>本次运行没有失败用例。</p>")
    parts.append("</section>")

    # ---------- 审计 ----------
    if audit_checks:
        parts.append("<section><h2>五、审计链完整性校验</h2>"
                     "<div class='hint'>每条审计事件都带上一条的哈希，"
                     "任何插入、删除或篡改都会让链条断裂并被定位到具体条数。</div>")
        parts.append("<table><thead><tr><th>审计流</th><th>事件数</th>"
                     "<th>校验结果</th></tr></thead><tbody>")
        for c in audit_checks:
            ok = c.get("ok")
            parts.append(
                f"<tr><td><code>{esc(c.get('name',''))}</code></td><td>{c.get('records',0)}</td>"
                f"<td>{'<span class="pill ok">完整</span>' if ok else '<span class="pill bad">断裂</span>'}"
                f" <span class='muted'>{esc(c.get('message',''))}</span></td></tr>")
        parts.append("</tbody></table></section>")

    parts.append("<footer>Agent 行为审计与回归评测系统 · 纯标准库实现 · 本报告自包含，可离线归档</footer>")
    parts.append("</div></body></html>")

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(parts), encoding="utf-8")
    return p
