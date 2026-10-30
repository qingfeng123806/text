/* 用最小 DOM 桩驱动 web/index.html 里的脚本，真连后端验证前端逻辑。
   用法：node tests/ui_smoke.js <http服务端口> [http|file]
   - http：控制台页面由服务提供（location.protocol = "http:"）
   - file：用户双击打开本地文件（location.protocol = "file:"），这是「初始化失败」的复现场景
*/
const fs = require("fs");
const vm = require("vm");

const PORT = process.argv[2] || "8793";
const MODE = process.argv[3] || "http";

class CL {
  constructor(){ this.s = new Set(); }
  add(...c){ c.forEach(x => this.s.add(x)); }
  remove(...c){ c.forEach(x => this.s.delete(x)); }
  contains(c){ return this.s.has(c); }
}
class El {
  constructor(sel){
    this.sel = sel; this._html = ""; this.textContent = ""; this.style = {};
    this.dataset = {}; this.classList = new CL(); this.disabled = false;
    this.value = ""; this.title = ""; this.className = "";
  }
  set innerHTML(v){ this._html = String(v); }
  get innerHTML(){ return this._html; }
}

const reg = new Map();
const mk = (sel) => { if(!reg.has(sel)) reg.set(sel, new El(sel)); return reg.get(sel); };
const NAV = ["overview", "cases", "regression", "audit"].map(t => {
  const e = new El("nav:" + t); e.dataset.tab = t; return e;
});
NAV[0].classList.add("on");
const TABS = ["overview", "cases", "regression", "audit"].map(t => {
  const e = new El("#tab-" + t); reg.set("#tab-" + t, e); return e;
});
TABS[0].classList.add("on");

const ORIGIN = MODE === "file" ? "null" : "http://127.0.0.1:" + PORT;
Object.defineProperty(globalThis, "document", {
  configurable: true, writable: true,
  value: {
    documentElement: { dataset: { theme: "light" } },
    querySelector: (s) => (s === "nav button.on"
      ? NAV.find(n => n.classList.contains("on")) : mk(s)),
    querySelectorAll: (s) => {
      if(s === "nav button") return NAV;
      if(s === ".tab") return TABS;
      return [];
    },
  },
});
Object.defineProperty(globalThis, "location", {
  configurable: true, writable: true,
  value: { protocol: MODE === "file" ? "file:" : "http:", search: "", origin: ORIGIN },
});
const ls = {};
Object.defineProperty(globalThis, "localStorage", {
  configurable: true, writable: true,
  value: {
    getItem: (k) => (k in ls ? ls[k] : null),
    setItem: (k, v) => { ls[k] = String(v); },
    removeItem: (k) => { delete ls[k]; },
  },
});
globalThis.window = globalThis;
globalThis.window.open = () => {};

const html = fs.readFileSync("web/index.html", "utf8");
const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
let code = blocks.sort((a, b) => b.length - a.length)[0];
code = code.replace(/\n\s*boot\(\);\s*$/, "\n");   // 去掉自启动，改由测试驱动
vm.runInThisContext(code, { filename: "index.html:script" });

const results = [];
const check = (name, cond, extra) => results.push([name, !!cond, extra || ""]);
const realFetch = globalThis.fetch;
const cut = () => { globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); }; };
const heal = () => { globalThis.fetch = realFetch; };

(async () => {
  console.log("=== 模式：" + MODE + " ===\n");

  /* ---- A. 服务可达：应完整渲染 ---- */
  await globalThis.boot();
  const ov = mk("#tab-overview").innerHTML;
  check("A1 连接成功（状态点变绿）", mk("#conn").className.includes("on"), mk("#conn").title);
  const run0 = (S.runs[0] || {}).run_id || "";   // 不写死 run_id，跟着实际产物走
  check("A2 运行下拉已填充", !!run0 && mk("#runSel").innerHTML.includes(run0), run0);
  check("A3 总览渲染出对比表", ov.includes("Agent 版本横向对比") && ov.includes("refund-agent-v1"));
  check("A4 总览不是错误框", !ov.includes("加载失败"), ov.slice(0, 50));
  check("A5 离线引导页保持隐藏", mk("#offline").style.display === "none");
  check("A6 静态报告按钮出现", mk("#reportBtn").style.display === "");

  await globalThis.refreshTab("cases", true);
  const cs = mk("#tab-cases").innerHTML;
  check("A7 用例页渲染", cs.includes("用例结果与行为轨迹") && cs.includes("PASS"));
  check("A8 用例页含轨迹回放", cs.includes("行为轨迹回放"));
  check("A9 用例页无「加载中」残留", !cs.includes("加载中"), cs.slice(0, 50));

  await globalThis.refreshTab("regression", true);
  const rg = mk("#tab-regression").innerHTML;
  check("A10 回归页渲染", rg.includes("REGRESSION") && rg.includes("新增回归"));

  await globalThis.refreshTab("audit", true);
  const au = mk("#tab-audit").innerHTML;
  check("A11 审计页渲染", au.includes("审计链完整性校验") && au.includes("全局不变式"));
  check("A12 审计链校验通过", au.includes("全部完整"));

  /* ---- B. 服务不可达：应给可操作的引导，而不是一句「初始化失败」---- */
  delete ls["agent-audit.api-base"];
  cut();
  await globalThis.boot();
  const guide = mk("#offline").innerHTML;
  check("B1 状态点变红", mk("#conn").className.includes("err"));
  check("B2 显示离线引导页", mk("#offline").style.display === "block");
  check("B3 引导页给出启动命令", guide.includes("python run.py serve"));
  check("B4 引导页可手动填服务地址", guide.includes("baseInput"));
  check("B5 引导页不是原始报错堆栈", !guide.includes("TypeError"));
  if(MODE === "file"){
    check("B6 file:// 下点明根因", guide.includes("你是用") && guide.includes("file://"),
          guide.includes("你是用") ? "已提示" : "缺少 file:// 专属提示");
  }
  heal();

  let bad = 0;
  for(const [name, ok, extra] of results){
    if(!ok) bad++;
    console.log((ok ? "  PASS  " : "  FAIL  ") + name + (extra ? "   [" + extra + "]" : ""));
  }
  console.log("\n合计 " + results.length + " 项，失败 " + bad + " 项");
  process.exit(bad ? 1 : 0);
})().catch(e => { console.error("harness 异常：", e); process.exit(2); });
