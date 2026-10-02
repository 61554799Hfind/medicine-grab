# -*- coding: utf-8 -*-
r"""
search_crawl.py 的「保守提速版」——  不改抓取方式，只砍掉浪费掉的时间。

和 fast_crawl.py 的区别
------------------------------------------------------------------
fast_crawl.py       直连站点 JSON 接口，最快（约 20~30 分钟），但有触发反爬的风险
本文件              还是「打开网页 → 填搜索框 → 点周期按钮 → 读表格」，
                    和你现在跑通的那套完全一样，因此风险最低，
                    只是把里面大量没必要的固定 sleep 换成了"等真正需要的东西"

旧版每味药 ≈ 30 秒，钱花在哪（511 味 × 30s ≈ 4.2 小时）
------------------------------------------------------------------
  page.goto() 整页重新加载 .................. ~3.0s
  加载后固定 wait_for_timeout(3000~4500) ..... ~3.8s   ← 纯浪费
  clear() 后等 500~1000ms ................... ~0.8s   ← 纯浪费
  fill() 后等 500~1000ms .................... ~0.8s   ← 纯浪费
  点搜索后等 1000ms ......................... ~1.0s
  点『类别-全部』+ 等 800~1200ms ............. ~1.8s   ← 只需点一次
  点『涨落-全部』+ 等 800~1200ms ............. ~1.8s   ← 只需点一次
  5 个周期 × (点击 + 等 800~1200ms) .......... ~5.0s
  逐单元格 inner_text() 取表格 .............. ~1.0s   ← 改成一次 JS 取完
  结尾 time.sleep(8~14) .................... ~11.0s  ← 最大头，纯浪费
  ----------------------------------------------------------
  合计 ≈ 30s，其中约 19s 是可以省掉的

本版的做法
------------------------------------------------------------------
  1. 页面只加载一次，之后每味药只「清输入框 → 输入 → 点搜索」，不再 goto
  2. 『类别-全部』『涨落-全部』只在开头点一次（它们本来就是全局筛选条件）
  3. 所有固定 sleep 换成"等网络空闲 / 等表格变化"的条件等待
  4. 表格用一次 page.evaluate() 整表取出，不再一个单元格一个单元格地问
  5. 每味药结尾的 8~14 秒改为 2 秒

  预计 ≈ 8~10s/味 × 511 味 ≈ 70~85 分钟（约快 3 倍）

用法
------------------------------------------------------------------
  .venv\Scripts\python.exe search_crawl_slim.py --limit 5    # 先试 5 味
  .venv\Scripts\python.exe search_crawl_slim.py              # 正式跑
  .venv\Scripts\python.exe search_crawl_slim.py --gap 4.0    # 被封时把间隔调大
"""
import argparse
import csv
import os
import random
import time
from datetime import datetime

from playwright.sync_api import sync_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MEDICINE_FILE = os.path.join(BASE_DIR, "medicine_list.txt")
OUTPUT_CSV = os.path.join(BASE_DIR, "药材涨落对比结果.csv")
FAILED_TXT = os.path.join(BASE_DIR, "failed_medicines.txt")

EDGE_PROFILE = r"D:\medicine_grab\edge_profile"
EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
TARGET_URL = "https://www.yt1998.com/priceUpDown.html"
PROXY_URL = "http://127.0.0.1:3213"

PERIODS = [("日", "yesterdayClick", "昨日对比"), ("周", "zhouduibiClick", "周对比"),
           ("月", "yueduibiClick", "月对比"), ("季度", "jiduibiClick", "季度对比"),
           ("年", "nianduibiClick", "年对比")]
HEADER = ["品名", "规格", "产地", "原价", "现价(元/kg)", "走势",
          "昨日对比", "周对比", "月对比", "季度对比", "年对比", "更新日期"]

# 一次性把整张表取出来的 JS，避免成百上千次 CDP 往返
JS_TABLE = """
() => Array.from(document.querySelectorAll('table.tab11 tbody#priceList tr')).map(tr => {
    const td = Array.from(tr.querySelectorAll('td')).map(x => x.innerText.trim());
    if (td.length < 7) return null;
    if (!td[1] && !td[2]) return null;
    return {规格: td[1], 产地: td[2], 原价: td[3], 现价: td[4], 走势: td[5], 涨跌幅: td[6]};
}).filter(Boolean)
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 味（调试用）")
    ap.add_argument("--gap", type=float, default=2.0, help="每味药之间的间隔秒数")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    with open(MEDICINE_FILE, "r", encoding="utf-8") as f:
        names = [ln.strip() for ln in f if ln.strip()]
    seen, meds = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            meds.append(n)
    if args.limit:
        meds = meds[:args.limit]

    today = datetime.now().strftime("%Y-%m-%d")
    all_rows, failed = [], []
    t0 = time.time()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=EDGE_PROFILE, executable_path=EDGE_EXE, headless=False,
            args=["--no-proxy-server"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
            viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()

        # ---- 页面只加载一次 ----
        page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded")
        page.wait_for_selector("input#kw", timeout=30000)
        page.add_style_tag(content=".ad,.advertissement,[id^='ad'],[class^='ad'],"
                                   ".float-ad,#floatAd,.popup{display:none!important}")
        # 全局筛选只设一次
        for sel in ["a[onclick*=\"toSearch('leibie','')\"]",
                    "a[onclick*=\"toSearch('ascFlag','')\"]"]:
            try:
                page.locator(sel).first.click(force=True, timeout=3000)
                page.wait_for_timeout(600)
            except Exception:
                pass

        for i, name in enumerate(meds, 1):
            print(f"\n==== [{i}/{len(meds)}] {name} ====")
            try:
                base, order = {}, []
                for period, pid, col in PERIODS:
                    try:
                        page.locator(f"a#{pid}").click(force=True, timeout=5000)
                    except Exception:
                        continue
                    page.wait_for_timeout(400)          # 给请求一点起飞时间
                    rows = None
                    for _ in range(30):                 # 轮询等表格，最多 6 秒
                        try:
                            rows = page.evaluate(JS_TABLE)
                        except Exception:
                            rows = None
                        if rows:
                            break
                        page.wait_for_timeout(200)
                    if not rows:
                        continue
                    for r in rows:
                        key = f"{r['规格']}_{r['产地']}"
                        if key not in base:
                            base[key] = dict(r)
                            order.append(key)
                        base[key][col] = r["涨跌幅"]

                if base:
                    for k in order:
                        b = base[k]
                        all_rows.append([name, b["规格"], b["产地"], b["原价"],
                                         b["现价"], b["走势"], b.get("昨日对比", ""),
                                         b.get("周对比", ""), b.get("月对比", ""),
                                         b.get("季度对比", ""), b.get("年对比", ""), today])
                    print(f"  ✅ {len(order)} 行")
                else:
                    failed.append(f"{name} (无数据)")
                    print("  ⚠️ 无数据")
            except Exception as e:
                failed.append(f"{name} (异常:{str(e)[:30]})")
                print(f"  ❌ 异常 {e!r}")

            if i % 10 == 0:
                el = time.time() - t0
                print(f"  进度 {i}/{len(meds)} 已用 {el/60:.1f}min "
                      f"预计剩余 {(len(meds)-i)*el/i/60:.1f}min")
            time.sleep(args.gap)
        ctx.close()

    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        w.writerows(all_rows)
    if failed:
        with open(FAILED_TXT, "w", encoding="utf-8") as f:
            f.write("以下药材未能获取到数据：\n" + "\n".join(failed))
    print(f"\n完成：{len(all_rows)} 行，失败 {len(failed)} 味，"
          f"耗时 {(time.time()-t0)/60:.1f} 分钟")

    if not args.no_push:
        os.environ["HTTP_PROXY"] = PROXY_URL
        os.environ["HTTPS_PROXY"] = PROXY_URL
        try:
            from push_to_gsheet import push_data
            push_data(HEADER, all_rows, failed_list=failed)
            print("Google Sheet 推送成功")
        except Exception as e:
            print(f"⚠️ 推送失败：{e}")
        finally:
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)


if __name__ == "__main__":
    main()
