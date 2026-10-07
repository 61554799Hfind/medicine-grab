// ============================================================
// 用途：由 Cloudflare Workers 定时调用 GitHub Actions 的
//       workflow_dispatch 接口，触发药材价格抓取。
//
// 令牌不写在代码里，而是存在 Cloudflare 的「变量和密钥」中，
// 变量名固定为 GH_DISPATCH_TOKEN（见配套文档）。
//
// 部署方式：浏览器里粘贴到 Cloudflare 的代码编辑器，点 Deploy。
//   定时执行 -> scheduled() 函数
//   用浏览器打开 Worker 网址 -> fetch() 函数（手动测试用）
// ============================================================

const REPO = "61554799Hfind/medicine-grab";
const WORKFLOW = "daily-crawl.yml";

async function dispatchToGitHub(env) {
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;

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

  // GitHub 成功时返回 204（无内容），失败时返回一段 JSON 错误信息
  const text = await res.text();
  return { status: res.status, body: text.slice(0, 300) };
}

export default {
  // 到点自动触发（时间在 Cron Triggers 里配置）
  async scheduled(event, env, ctx) {
    const r = await dispatchToGitHub(env);
    console.log(`[定时触发] status=${r.status} body=${r.body}`);
  },

  // 浏览器打开 Worker 网址时触发一次，用来验证配置是否正确
  async fetch(request, env, ctx) {
    const r = await dispatchToGitHub(env);
    const tip =
      r.status === 204
        ? "✅ 成功！GitHub 已收到触发请求，去 Actions 页面看新运行。"
        : r.status === 401 || r.status === 403
          ? "❌ 令牌不对，或没给 Actions: Read and write 权限。"
          : r.status === 404
            ? "❌ 仓库名或 workflow 文件名写错了。"
            : "⚠️ 没见过的返回码，把下面内容发出来看看。";
    return new Response(`${tip}\nstatus: ${r.status}\n${r.body}\n`, {
      headers: { "content-type": "text/plain; charset=utf-8" },
    });
  },
};
