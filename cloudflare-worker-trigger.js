// ============================================================
// 用途：Cloudflare Workers 定时调用 GitHub Actions 的 workflow_dispatch 接口，
//       让每天几条抓取线在**早上的准点**跑起来（GitHub 自己的 cron 在本仓库长期迟到 5~8 小时）。
//
// 每天的顺序（北京时间）：
//   08:05 供应商  ──dispatch──▶ supplier-daily.yml
//                    └─跑完自动接▶ 每日康美药材价格（workflow_run）
//                                  └─跑完自动接▶ 每日中香网香料价格（workflow_run）
//   08:35 求购    ──dispatch──▶ purchase-daily.yml
//   08:45 药材价格 ──dispatch──▶ daily-crawl.yml（约 1 小时，独占配额）
//
// 所以 Worker 只需要打 3 个 dispatch；康美、中香两条靠 GitHub 的 workflow_run 接力，不用单独配 cron。
// （康美必须用阿里云 FC 抓数据，但**推表**这一步还是在 GitHub 上跑，所以它照样要被触发。）
//
// ⚠️ 2026-11-18 修的坑：原来按 event.cron 的**原始字符串**查表，Cloudflare 触发器里若写成
//    "05 0 * * *"（带前导零）或多余空格，就匹配不上 → 兜底 daily-crawl.yml，
//    表现就是「不管配了几条，只有药通网那条准时跑」。现在 norm() 先压空格、再去前导零。
//    v3 起：直接打开 Worker 网址只做自检、不再误触发任何 workflow；要测试必须带 /?wf=xxx。
//
// 部署：浏览器里把整段粘到 Cloudflare Workers 的编辑器，点 Deploy。
// 然后在 Worker「设置 → 触发器 → Cron 触发器」里加下面 ROUTES 里的三条（UTC），表达式必须一模一样。
// 令牌不写代码里，存在「变量和密钥」，名字固定 GH_DISPATCH_TOKEN。
// ============================================================

const REPO = "61554799Hfind/medicine-grab";
const BUILD = "v3 2026-11-18 去前导零+自检页不误触发";

// 键是 UTC 表达式；注释是北京时间。改时间要同时改这里和 Cloudflare 触发器，两边必须一致。
const ROUTES = {
  "5 0 * * *":  { wf: "supplier-daily.yml", label: "供应商 药通网+康美+中香（北京 08:05，6~8 分钟）→ 自动接康美、中香" },
  "35 0 * * *": { wf: "purchase-daily.yml", label: "个人求购 药通网+中香（北京 08:35，1~2 分钟）" },
  "45 0 * * *": { wf: "daily-crawl.yml",    label: "药材价格（北京 08:45，约 1 小时）" },
};

// 匹配不上时的兜底（宁可多跑一次价格线，也不要静默什么都不做）
const FALLBACK_WF = "daily-crawl.yml";

// 手动测试用：/ ?wf=xxx
const ALIAS = {
  supplier: "supplier-daily.yml",
  purchase: "purchase-daily.yml",
  price: "daily-crawl.yml",
  spice: "spice-daily.yml",
  zx: "zx-price-daily.yml",
};

// Cloudflare 可能把 "5 0 * * *" 传成 "05 0 * * *" 或多空格，统一压空格 + 去分/时前导零再比
const norm = (s) => String(s || "").trim().split(/\s+/).map((x, i) => {
  if (i < 2 && /^0\d$/.test(x)) return String(parseInt(x, 10));  // "05" → "5"
  return x;
}).join(" ");

async function dispatch(env, workflow) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${workflow}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
      "Content-Type": "application/json",
      "User-Agent": "medicine-cron-trigger",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  const text = await res.text();
  return { status: res.status, body: text.slice(0, 300) };
}

export default {
  // 到点自动触发（时间在 Cloudflare 的 Cron Triggers 里配）
  async scheduled(event, env, ctx) {
    const cron = norm(event.cron);
    const hit = ROUTES[cron];
    if (!hit) {
      console.log(
        `[定时] ⚠️ 未匹配 cron="${cron}"（原始 "${event.cron}"）→ 走兜底 ${FALLBACK_WF}。` +
        `请核对 Cloudflare 触发器里的表达式，应与 ROUTES 的键完全一致：${Object.keys(ROUTES).join(" / ")}`
      );
    } else {
      console.log(`[定时] cron="${cron}" → ${hit.wf}（${hit.label}）`);
    }
    const wf = hit ? hit.wf : FALLBACK_WF;
    const r = await dispatch(env, wf);
    console.log(`[定时] ${wf} status=${r.status} body=${r.body}`);
  },

  // 浏览器打开 Worker 网址：
  //   /                 → 配置自检（列出应有触发器 + 触发价格线）
  //   /routes           → 只看应有触发器清单（不触发任何 workflow）
  //   /?wf=supplier     → 立即触发供应商线（会连带康美、中香）
  //   /?wf=purchase     → 求购线
  //   /?wf=price        → 药材价格线
  //   /?wf=spice        → 只跑康美推表
  //   /?wf=zx           → 只跑中香推表
  async fetch(request, env, ctx) {
    const u = new URL(request.url);

    if (u.pathname === "/routes" || u.pathname === "/") {
      const lines = Object.entries(ROUTES).map(
        ([cron, v]) => `${cron}\t${v.wf}\t${v.label}`
      );
      const isRoot = u.pathname === "/";
      return new Response(
        `BUILD: ${BUILD}\n` +
        (isRoot ? `（本页只是自检，没有触发任何 workflow；要测试请加 /?wf=supplier 等参数）\n` : "") +
        `\n在 Cloudflare「触发器 → Cron 触发器」里必须有下面这 ${lines.length} 条（UTC，写 05 或 5 都能匹配）：\n\n` +
        lines.join("\n") +
        `\n\n可手动测试的入口：\n` +
        Object.entries(ALIAS).map(([k, v]) => `  /?wf=${k}\t→ ${v}`).join("\n") +
        `\n`,
        { headers: { "content-type": "text/plain; charset=utf-8" } }
      );
    }

    const q = (u.searchParams.get("wf") || "").toLowerCase();
    const wf = ALIAS[q] || FALLBACK_WF;
    const r = await dispatch(env, wf);
    const tip =
      r.status === 204
        ? `✅ 成功！已触发 ${wf}，去 GitHub Actions 页面看新运行。`
        : r.status === 401 || r.status === 403
          ? "❌ 令牌不对，或没给 Actions: Read and write 权限。"
          : r.status === 404
            ? `❌ 仓库名或 workflow 文件名写错了（${wf}）。`
            : "⚠️ 没见过的返回码，把下面内容发出来看看。";
    return new Response(`${tip}\nworkflow: ${wf}\ncron(应有): ${Object.keys(ROUTES).join(" / ")}\nstatus: ${r.status}\n${r.body}\n`, {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
