# -*- coding: utf-8 -*-
r"""
药材涨落数据「快速抓取」脚本 —— search_crawl.py 的提速版

原理
------------------------------------------------------------------
站点点的那 5 个「日/周/月/季/年」按钮，其实都是在请求同一个 JSON 接口：
    /price/nowDayPriceQ!getZhangLuo.do
直接打这个接口，单次 0.08~0.10 秒，不用渲染页面、不用点按钮、不用解析 DOM。

旧版 search_crawl.py 每味药 ≈ 30 秒（511 味 ≈ 4 小时），其中约 19 秒是
固定 sleep 和整页重载；本版实测 ≈ 15~20 分钟跑完全部。

⚠️ 关键：必须用「你自己的 Edge 用户配置」
------------------------------------------------------------------
站点的 WAF 会拦异常客户端：
    · Python requests 直连        → 返回空 body（被拦）
    · Playwright 新建的临时配置   → 返回反爬 HTML（被拦）
    · 你自己的 edge_profile       → 正常
所以本脚本默认用 launch_persistent_context 加载 D:\medicine_grab\edge_profile。
跑之前请先关掉已打开的 Edge（同一个配置不能同时被两个实例占用）。

速率（实测 2026-10-02）
------------------------------------------------------------------
    间隔 0.50s（约1.6次/秒）  50/50 成功
    间隔 0.30s（约2.6次/秒）  50/50 成功
    间隔 0.15s（约4.1次/秒）  50/50 成功
    间隔 0.05s（约7.8次/秒）  30/50，开始被限流
被拦是「滑动窗口限流」，退避后会自动恢复，不是永久封禁。
脚本内置 AIMD 自适应：被挡就自动放慢，顺畅了再慢慢提速。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe fast_crawl.py --limit 20     # 先试 20 味
    .venv\Scripts\python.exe fast_crawl.py                # 全量（推荐）
    .venv\Scripts\python.exe fast_crawl.py --turbo        # 更快（更易被限流）
    .venv\Scripts\python.exe fast_crawl.py --slow         # 更稳
    .venv\Scripts\python.exe fast_crawl.py --resume       # 断点续跑
    .venv\Scripts\python.exe fast_crawl.py --head         # 显示浏览器窗口
"""
import argparse
import csv
import json
import os
import random
import re
import time
from datetime import datetime
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright

# ============================ 配置 ============================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDICINE_FILE = os.path.join(BASE_DIR, "medicine_list.txt")
OUTPUT_CSV = os.path.join(BASE_DIR, "药材涨落对比结果.csv")
FAILED_TXT = os.path.join(BASE_DIR, "failed_medicines.txt")
PROGRESS_JSON = os.path.join(BASE_DIR, "crawl_progress.json")
LOG_FILE = os.path.join(BASE_DIR, "crawl.log")

# --- 本机 Edge 配置（可选：服务器上没有就用 Playwright 自带 Chromium）---
EDGE_PROFILE = os.environ.get("EDGE_PROFILE", r"D:\medicine_grab\edge_profile")
EDGE_EXE = os.environ.get("EDGE_EXE",
                          r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
# 服务器上使用的持久化配置目录（Linux 下默认 ./browser_profile）
SERVER_PROFILE = os.environ.get("BROWSER_PROFILE",
                                os.path.join(BASE_DIR, "browser_profile"))
CDP_PORT = 9222

# ⚠️ WAF 通行证：站点只检查这个 cookie 是否存在且非空，值可以随便填。
#    实测（2026-10-02）：注入任意 8 位随机字符串即可放行；空字符串会被拦。
PASS_COOKIE_NAME = "zshcookiename"

BASE_URL = "https://www.yt1998.com/price/nowDayPriceQ!getZhangLuo.do"
ENTRY_URL = "https://www.yt1998.com/priceUpDown.html"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

# 五个周期：接口参数名 -> 输出列名
PERIODS = [("yesterday", "昨日对比"), ("zhouduibi", "周对比"), ("yueduibi", "月对比"),
           ("jiduibi", "季度对比"), ("nianduibi", "年对比")]
HEADER = ["品名", "规格", "产地", "原价", "现价(元/kg)", "走势",
          "昨日对比", "周对比", "月对比", "季度对比", "年对比", "更新日期"]

# 节奏档位：(请求间隔秒, 每味药之间的间隔秒)
SPEED = {
    "normal": (0.25, 0.20),
    "turbo":  (0.12, 0.10),
    "slow":   (0.80, 1.00),
}

PAGE_SIZE = 200          # 单页最多取多少条
MAX_RETRY = 5            # 单个请求最大重试次数
ABORT_AFTER_BLOCKS = 30  # 连续被挡这么多次就停（多半是真被封了）
TAG_RE = re.compile(r"<[^>]+>")
# ==============================================================


class Crawler:
    def __init__(self, speed="normal", headless=True, use_cdp=False):
        self.base_gap, self.med_gap = SPEED[speed]
        self.gap = self.base_gap          # 当前实际间隔，会被自适应调整
        self.headless = headless
        self.use_cdp = use_cdp
        self.log_fh = open(LOG_FILE, "a", encoding="utf-8")
        self.consec_block = 0             # 连续被挡次数
        self.aborted = False
        self.stats = {"req": 0, "ok": 0, "block": 0, "retry": 0}
        self.t_start = time.time()

    # ---------- 日志 ----------
    def log(self, msg=""):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        try:                      # 收尾阶段日志文件可能已关闭，不能因此崩掉
            self.log_fh.write(line + "\n")
            self.log_fh.flush()
        except Exception:
            pass

    # ---------- 启动浏览器（只当网络栈用） ----------
    def start(self):
        self.pw = sync_playwright().start()
        self.browser = None

        # 方式1：连到已经开着的 Edge（run_all.bat 起的那个），最省事
        if self.use_cdp:
            try:
                self.browser = self.pw.chromium.connect_over_cdp(
                    f"http://127.0.0.1:{CDP_PORT}")
                self.ctx = self.browser.contexts[0]
                self.log(f"已连接到已运行的 Edge（端口 {CDP_PORT}）")
            except Exception as e:
                self.log(f"⚠️ 连接已运行 Edge 失败（{str(e)[:60]}），改为自己启动")
                self.browser = None

        # 方式2：本机能找到 Edge → 用你自己的持久化配置（最贴近真人环境）
        if self.browser is None:
            use_edge = os.path.exists(EDGE_EXE) and os.path.exists(EDGE_PROFILE)
            args = ["--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage"]
            if not os.environ.get("HTTP_PROXY") and not os.environ.get("HTTPS_PROXY"):
                args.append("--no-proxy-server")
            if use_edge:
                self.ctx = self.pw.chromium.launch_persistent_context(
                    user_data_dir=EDGE_PROFILE, executable_path=EDGE_EXE,
                    headless=self.headless, args=args)
                self.log(f"已加载本机 Edge 配置：{EDGE_PROFILE}")
            else:
                # 服务器 / CI 环境：Playwright 自带 Chromium + 持久化配置目录
                os.makedirs(SERVER_PROFILE, exist_ok=True)
                self.ctx = self.pw.chromium.launch_persistent_context(
                    user_data_dir=SERVER_PROFILE, headless=self.headless,
                    user_agent=UA, locale="zh-CN", args=args)
                self.log(f"未找到本机 Edge，改用 Chromium 配置：{SERVER_PROFILE}")

        # ---- 注入 WAF 通行证 cookie（关键）----
        # 站点只检查 zshcookiename 存在且非空，值任意。这样服务端无需搬运 profile。
        try:
            val = "".join(random.choice("abcdefghijklmnopqrstuvwxyz"
                                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
                          for _ in range(8))
            self.ctx.add_cookies([{
                "name": PASS_COOKIE_NAME, "value": val,
                "domain": ".yt1998.com", "path": "/"}])
            self.log(f"已注入通行 cookie {PASS_COOKIE_NAME}={val}")
        except Exception as e:
            self.log(f"⚠️ 通行 cookie 注入失败：{str(e)[:80]}")

        # 打开一次入口页面，拿到会话 cookie。
        # 全新 IP（服务器 / CI 首次运行）第一次访问常被拦，所以重试几次。
        for attempt in range(4):
            try:
                pg = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
                pg.goto(ENTRY_URL, wait_until="domcontentloaded", timeout=45000)
                pg.wait_for_timeout(2500)
                ln = len(pg.content())
                if ln > 10000:                     # 正常页面约 38KB，被拦时只有 39 字节
                    self.log(f"入口页面就绪（title={pg.title()!r} len={ln}）")
                    break
                self.log(f"⚠️ 入口页异常（len={ln}），第 {attempt + 1}/4 次重试…")
            except Exception as e:
                self.log(f"⚠️ 入口页面打开异常：{str(e)[:80]}")
            if attempt < 3:
                time.sleep(5 * (attempt + 1))
        self.api = self.ctx.request

    def stop(self):
        try:
            if self.browser:
                self.browser.close()
            else:
                self.ctx.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass

    def close_log(self):
        try:
            self.log_fh.close()
        except Exception:
            pass

    # ---------- 自适应节奏 ----------
    def on_success(self):
        self.consec_block = 0
        # 顺畅了就慢慢往回提速（每次缩短 5%，不低于基准）
        self.gap = max(self.base_gap, self.gap * 0.95)

    def on_blocked(self):
        self.consec_block += 1
        self.stats["block"] += 1
        # 被挡就放慢：间隔翻倍，最多 8 秒
        self.gap = min(8.0, self.gap * 2)
        wait = min(30, 2 * self.consec_block)
        return wait

    # ---------- 单个接口请求 ----------
    def fetch(self, name, period, page_index=0):
        params = {"random": random.random(),
                  "ascFlag": "",              # 关键：空=全部；"zhang"=只看上涨，会漏数据
                  "ycnam": name, "market": "1", "leibie": "", "istoday": "0",
                  "spices": "", "tea": "", "paramName": period, "paramValue": 1,
                  "pageIndex": page_index, "pageSize": PAGE_SIZE}
        url = BASE_URL + "?" + urlencode(params, safe="")
        self.stats["req"] += 1
        try:
            r = self.api.get(url, timeout=30000)
            txt = r.text()
        except Exception as e:
            self.stats["retry"] += 1
            return None, f"EXC {e!r}"
        if not txt.strip().startswith("{"):
            return None, f"被挡(返回HTML/空, {len(txt)}字节)"
        try:
            j = json.loads(txt)
        except Exception:
            return None, "JSON解析失败"
        if not isinstance(j, dict) or "data" not in j:
            return None, "响应缺少data字段"
        self.stats["ok"] += 1
        return j, None

    def fetch_with_retry(self, name, period):
        for attempt in range(MAX_RETRY):
            j, err = self.fetch(name, period)
            if j is not None:
                self.on_success()
                return j
            wait = 0.4 * (attempt + 1)
            if err and "被挡" in err:
                wait = self.on_blocked()
                self.log(f"    ⚠️ 被限流（连续 {self.consec_block} 次），"
                         f"间隔提到 {self.gap:.2f}s，冷却 {wait}s")
            else:
                self.stats["retry"] += 1
            if self.consec_block >= ABORT_AFTER_BLOCKS:
                self.aborted = True
                self.log("🛑 连续被挡次数过多，已停止。稍后用 --resume 继续。")
                return None
            time.sleep(wait)
        return None

    # ---------- 抓一味药 ----------
    def crawl_one(self, name, try_names=None):
        """try_names: 按顺序尝试的查询名；第一个有数据的生效。"""
        base, order = {}, []
        used = None
        for period, col in PERIODS:
            j = None
            for qn in (try_names or [name]):
                if used and qn != used:      # 一旦某个名字命中，后续周期沿用
                    continue
                j = self.fetch_with_retry(qn, period)
                time.sleep(self.gap)
                if self.aborted:
                    return None
                if j is not None and (j.get("data") or []):
                    used = qn
                    break
                j = None
            if j is None:
                continue
            rows = j.get("data") or []
            total = j.get("total") or 0
            if total > len(rows) and len(rows) == PAGE_SIZE:   # 分页兜底
                idx = 1
                while len(rows) < total and idx < 10:
                    more, _ = self.fetch(name, period, page_index=idx)
                    if not more or not more.get("data"):
                        break
                    rows += more["data"]
                    idx += 1
                    time.sleep(self.gap)
            for d in rows:
                spec = (d.get("guige") or "").strip()
                origin = (d.get("chandi") or "").strip()
                key = f"{spec}_{origin}"
                if key not in base:
                    base[key] = {"规格": spec, "产地": origin,
                                 "原价": (d.get("price") or "").strip(),
                                 "现价(元/kg)": (d.get("pri") or "").strip(),
                                 "走势": (d.get("zoushi") or "").strip()}
                    order.append(key)
                base[key][col] = TAG_RE.sub("", d.get("zhangluo") or "").strip()
        if used and used != name:
            self.log(f"  🔄 {name} 站点无此名，已用别名「{used}」取到 {len(order)} 行")
        return [[name, base[k].get("规格", ""), base[k].get("产地", ""), base[k].get("原价", ""),
                 base[k].get("现价(元/kg)", ""), base[k].get("走势", ""), base[k].get("昨日对比", ""),
                 base[k].get("周对比", ""), base[k].get("月对比", ""), base[k].get("季度对比", ""),
                 base[k].get("年对比", "")] for k in order]


def load_medicines(limit=None):
    with open(MEDICINE_FILE, "r", encoding="utf-8") as f:
        names = [ln.strip() for ln in f if ln.strip()]
    seen, out = set(), []
    for n in names:                      # 去重（列表里「干姜」重复了一次）
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out[:limit] if limit else out


def load_alias():
    """读 alias.txt，返回 {原名: [别名1, 别名2, ...]}"""
    path = os.path.join(BASE_DIR, "alias.txt")
    m = {}
    if not os.path.exists(path):
        return m
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cands = [x.strip() for x in v.split(",") if x.strip()]
            if k.strip() and cands:
                m[k.strip()] = cands
    return m


def save_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def push_gsheet(header, rows, failed, proxy_url=None):
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
    try:
        from push_to_gsheet import push_data
        status = "正常" if not failed else f"部分失败({len(failed)}个)"
        push_data(header, rows, failed_list=failed, status_msg=status)
        return True
    except Exception as e:
        print(f"⚠️ 推送 Google Sheet 失败：{e}")
        return False
    finally:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="只抓前 N 味（试跑；结果写到单独文件，不会覆盖正式 CSV）")
    ap.add_argument("--turbo", action="store_true", help="更快的节奏")
    ap.add_argument("--slow", action="store_true", help="更保守的节奏")
    ap.add_argument("--resume", action="store_true", help="断点续跑，跳过已完成的")
    ap.add_argument("--no-push", action="store_true", help="不推送 Google Sheet")
    ap.add_argument("--head", action="store_true", help="显示浏览器窗口（调试用）")
    ap.add_argument("--cdp", action="store_true",
                    help="连接到已运行的 Edge（需先按 run_all.bat 方式启动）")
    ap.add_argument("--alias", action="store_true",
                    help="启用 alias.txt：站点查不到时自动尝试别名（如 枸杞->枸杞子）")
    args = ap.parse_args()

    alias_map = load_alias() if args.alias else {}
    if alias_map:
        print(f"已启用别名表：{len(alias_map)} 条")

    speed = "turbo" if args.turbo else ("slow" if args.slow else "normal")
    meds = load_medicines(args.limit or None)

    # 试跑模式：结果和断点都写到独立文件，绝不覆盖正式产出
    trial = bool(args.limit)
    out_csv = OUTPUT_CSV
    progress_json = PROGRESS_JSON
    if trial:
        out_csv = os.path.join(BASE_DIR, "试跑结果.csv")
        progress_json = os.path.join(BASE_DIR, "crawl_progress_trial.json")

    done = {}
    if args.resume and os.path.exists(progress_json):
        try:
            with open(progress_json, "r", encoding="utf-8") as f:
                done = json.load(f)
            print(f"读取断点：已完成 {len(done)} 味，将跳过")
        except Exception:
            done = {}

    today = datetime.now().strftime("%Y-%m-%d")
    all_rows, failed = [], []
    c = Crawler(speed=speed, headless=not args.head, use_cdp=args.cdp)
    c.log(f"===== 开始：{len(meds)} 味药，节奏={speed} "
          f"(请求间隔 {c.base_gap}s / 每味间隔 {c.med_gap}s) =====")
    c.start()
    t0 = time.time()
    try:
        for i, name in enumerate(meds, 1):
            if args.resume and name in done:
                all_rows += done[name]["rows"]
                if not done[name].get("rows"):
                    failed.append(f"{name} (无数据)")
                continue
            try:
                rows = c.crawl_one(name, [name] + alias_map.get(name, []))
            except Exception as e:
                c.log(f"  ❌ {name} 异常：{e!r}")
                rows = []

            if c.aborted:
                c.log("🛑 检测到持续拦截，提前结束（已存盘，稍后 --resume 继续）")
                break

            if rows:
                all_rows += rows
                done[name] = {"rows": rows}
            else:
                failed.append(f"{name} (无数据)")
                done[name] = {"rows": []}
                c.log(f"  ⚠️ {name} 五个周期均无数据")

            if i % 10 == 0 or i == len(meds):
                el = time.time() - t0
                rate = i / el if el else 0
                eta = (len(meds) - i) / rate if rate else 0
                c.log(f"  进度 {i}/{len(meds)} | 已用 {el/60:.1f}min | "
                      f"{rate:.2f} 味/s | 预计剩余 {eta/60:.1f}min | "
                      f"成功请求 {c.stats['ok']} 被挡 {c.stats['block']} "
                      f"当前间隔 {c.gap:.2f}s")
                with open(progress_json, "w", encoding="utf-8") as f:
                    json.dump(done, f, ensure_ascii=False)
                save_csv(out_csv, HEADER, [[*r, today] for r in all_rows])
            time.sleep(c.med_gap)
    finally:
        c.stop()
        with open(progress_json, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)

    el = time.time() - t0
    final_rows = [[*r, today] for r in all_rows]
    save_csv(out_csv, HEADER, final_rows)

    if failed:
        with open(FAILED_TXT, "w", encoding="utf-8") as f:
            f.write("以下药材未能获取到数据：\n")
            f.write("\n".join(failed))
    elif os.path.exists(FAILED_TXT):
        os.remove(FAILED_TXT)

    c.log("=" * 60)
    c.log(f"抓取完成：{len(final_rows)} 行 / {len(meds) - len(failed)} 味成功，"
          f"失败 {len(failed)} 味")
    c.log(f"总耗时 {el/60:.1f} 分钟（旧版约 240 分钟）")
    c.log(f"请求统计：共 {c.stats['req']} 次，成功 {c.stats['ok']}，"
          f"被挡 {c.stats['block']}，其他重试 {c.stats['retry']}")
    c.log(f"CSV 已保存：{out_csv}")

    if trial:
        c.log("（试跑模式：正式 CSV 与 Google Sheet 均未改动）")
    elif not args.no_push:
        # 代理只在需要时才用：本机走 127.0.0.1:3213，境外服务器留空直连
        proxy = os.environ.get("GSHEET_PROXY", "http://127.0.0.1:3213")
        ok = push_gsheet(HEADER, final_rows, failed, proxy_url=proxy)
        c.log("Google Sheet 推送" + ("成功" if ok else "失败（本地 CSV 仍在）"))
    c.close_log()


if __name__ == "__main__":
    main()
