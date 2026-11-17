// ============================================================
// 用途：Cloudflare Workers 定时调用 GitHub Actions 的 workflow_dispatch 接口，
//       让三条每日抓取线在**早上的准点**跑起来。
//
// 为什么要这个：GitHub 自己的 cron（schedule）在这个仓库上长期迟到 6~8 小时。
//   实测 2026-11-11 ~ 11-17：
//     cron 写 UTC 00:05（北京 08:05）→ 实际 13:59 / 15:00 / 14:29 / 14:08 / 14:15 / 14:13 / 15:47 才启动
//     cron 写 UTC 00:17（北京 08:17）→ 实际 14:10 / 15:06 / 14:40 / 14:16 / 14:24 / 14:22 / 15:51 才启动
//   而 workflow_dispatch 是**即时的**：创建时间和启动时间同一秒（今天手动测试已证实）。
//   所以：准点靠这个 Worker，GitHub 的 cron 留作兜底（迟到了也没事，价格线会自己跳过）。
//
// 部署：浏览器里把下面整段粘到 Cloudflare Workers 的代码编辑器，点 Deploy。
// 然后在 Worker 的「设置 → 触发器 → Cron 触发器」里加下面 ROUTES 里的三条（UTC 时间）。
// 令牌不写在代码里，存在「变量和密钥」里，名字固定 GH_DISPATCH_TOKEN。
// ============================================================

const REPO = "61554799Hfind/medicine-grab";

// Cron 表达式是 UTC；注释里是北京时间。
// 想改时间：先改这里，再去「触发器」里改成一样的表达式，两边必须一致。
const ROUTES = {
  "5 0 * * *":  { wf: "supplier-daily.yml", label: "供应商 药通网+中香（北京 08:05）" },
  "17 0 * * *": { wf: "daily-crawl.yml",    label: "药材价格（北京 08:17）" },
  "35 0 * * *": { wf: "purchase-daily.yml", label: "个人求购 药通网+中香（北京 08:35）" },
};

// 康美那条不由这里管：它连不上海外 IP，由阿里云函数每天 07:50 抓完直接提交 CSV。
const FALLBACK_WF = "daily-crawl.yml";

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
  // 到点自动触发（时间在 Cloudflare 的 Cron Triggers 里配置）
  async scheduled(event, env, ctx) {
    const cron = event.cron || "";
    const hit = ROUTES[cron];
    const wf = hit ? hit.wf : FALLBACK_WF;
    console.log(`[定时] cron="${cron}" → ${wf}（${hit ? hit.label : "未匹配，走兜底"}）`);
    const r = await dispatch(env, wf);
    console.log(`[定时] status=${r.status} body=${r.body}`);
  },

  // 浏览器打开 Worker 网址时跑一次，用来验证配置。
  //   /               → 触发价格线
  //   /?wf=supplier   → 触发供应商线
  //   /?wf=purchase   → 触发求购线
  async fetch(request, env, ctx) {
    const q = new URL(request.url).searchParams.get("wf") || "";
    const wf =
      q === "supplier" ? "supplier-daily.yml"
      : q === "purchase" ? "purchase-daily.yml"
      : q === "price" ? "daily-crawl.yml"
      : FALLBACK_WF;

    const r = await dispatch(env, wf);
    const tip =
      r.status === 204
        ? `✅ 成功！已触发 ${wf}，去 GitHub Actions 页面看新运行。`
        : r.status === 401 || r.status === 403
          ? "❌ 令牌不对，或没给 Actions: Read and write 权限。"
          : r.status === 404
            ? `❌ 仓库名或 workflow 文件名写错了（${wf}）。`
            : "⚠️ 没见过的返回码，把下面内容发出来看看。";
    return new Response(`${tip}\nworkflow: ${wf}\nstatus: ${r.status}\n${r.body}\n`, {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
