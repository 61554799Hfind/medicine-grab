# -*- coding: utf-8 -*-
r"""
历史价格「全量日数据」抓取脚本
------------------------------------------------------------------
数据源：药通网历史价格接口（未登录也能拿 JSON）
    /price/historyPriceQ!getHistoryPrice.do
        ?ycnam=品名&guige=规格&chandi=产地&market=1&pageIndex=0&pageSize=2000
返回：{ state, pri, ycnam, guige, chandi,
        data:[ {Date_time, DayCapilization}, ... ] }   （逐日价格，约 3 年 / ~1080 点）
注意：三参数（ycnam/guige/chandi）缺一不可，否则 state=no。

种子：直接复用「药材涨落对比结果.csv」里已有的 (品名,规格,产地) 组合，
      不用再查涨落接口，省一半时间。

网络：复用 fast_crawl 的浏览器栈 + zshcookiename 通行 cookie（WAF 只认这个）。
      每组合只抓 1 次（接口一次返回全部历史），比涨落接口快得多。

产出：
    history_data.csv   长格式：品名,规格,产地,日期,价格   （供写 Google Sheet）
    history_data.json  按组合嵌套：{ "品名|规格|产地": [{"date","price"}...] }
    history_progress.json  断点（按组合）

用法：
    .venv/Scripts/python.exe history_crawl.py            # 全量
    .venv/Scripts/python.exe history_crawl.py --limit 20 # 试 20 个组合
    .venv\Scripts\python.exe history_crawl.py --resume   # 断点续跑
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

from fast_crawl import Crawler, BASE_DIR as FAST_BASE, PASS_COOKIE_NAME

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_CSV = os.path.join(BASE_DIR, "药材涨落对比结果.csv")
OUT_CSV = os.path.join(BASE_DIR, "history_data.csv")
OUT_JSON = os.path.join(BASE_DIR, "history_data.json")
PROGRESS = os.path.join(BASE_DIR, "history_progress.json")
LOG_FILE = os.path.join(BASE_DIR, "history_crawl.log")

HISTORY_URL = "https://www.yt1998.com/price/historyPriceQ!getHistoryPrice.do"
TAG_RE = re.compile(r"<[^>]+>")


class HistoryCrawler(Crawler):
    """复用 fast_crawl 的浏览器栈 + WAF cookie，只改抓取目标。"""

    def fetch_history(self, name, spec, origin, max_pages=40):
        """返回该组合的逐日价格列表 [(date, price), ...]，失败返回 ([], err)。"""
        all_pts = []
        seen_dates = set()
        for page in range(max_pages):
            params = {
                "random": random.random(),
                "ycnam": name, "guige": spec, "chandi": origin,
                "market": "1", "pageIndex": page, "pageSize": 2000,
            }
            url = HISTORY_URL + "?" + urlencode(params, safe="")
            try:
                r = self.api.get(url, timeout=30000)
                txt = r.text()
            except Exception as e:
                return all_pts, f"EXC {e!r}"
            if not txt.strip().startswith("{"):
                return all_pts, f"被挡(返回 {len(txt)} 字节非JSON)"
            try:
                j = json.loads(txt)
            except Exception:
                return all_pts, "JSON解析失败"
            if not isinstance(j, dict):
                return all_pts, "响应非对象"
            data = j.get("data") or []
            if not data and page == 0:
                # 第一页就空 → 这个组合在站点上没历史
                return [], "无数据(state=%s)" % j.get("state")
            for d in data:
                dt = (d.get("Date_time") or "").strip()
                price = TAG_RE.sub("", str(d.get("DayCapilization") or "")).strip()
                if not dt or dt in seen_dates:
                    continue
                seen_dates.add(dt)
                all_pts.append((dt, price))
            # 翻页判断：本页不足 pageSize 说明到底了
            if len(data) < 2000:
                break
        return all_pts, None


def load_combos(limit=None):
    """从涨落结果 CSV 读取 (品名,规格,产地) 唯一组合。"""
    combos = []
    seen = set()
    if not os.path.exists(SEED_CSV):
        raise SystemExit(f"找不到种子文件 {SEED_CSV}，请先跑一次 fast_crawl.py")
    with open(SEED_CSV, "r", encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        # 兼容列名（CSV 可能是 utf-8-sig，列名可能带 BOM）
        cols = {c: c for c in rd.fieldnames}
        name_col = next((c for c in rd.fieldnames if c.strip() == "品名"), "品名")
        spec_col = next((c for c in rd.fieldnames if c.strip() == "规格"), "规格")
        origin_col = next((c for c in rd.fieldnames if c.strip() == "产地"), "产地")
        for row in rd:
            n = (row.get(name_col) or "").strip()
            s = (row.get(spec_col) or "").strip()
            o = (row.get(origin_col) or "").strip()
            if not n:
                continue
            key = (n, s, o)
            if key in seen:
                continue
            seen.add(key)
            combos.append(key)
    return combos[:limit] if limit else combos


def load_alias():
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--incremental", action="store_true",
                    help="基于已有 history_data.json，只补齐缺失/新增的组合（用于加药材后增量更新）")
    ap.add_argument("--turbo", action="store_true")
    args = ap.parse_args()

    alias_map = load_alias()
    combos = load_combos(args.limit or None)
    print(f"待抓取组合（品名,规格,产地）共 {len(combos)} 个")

    done = {}
    if args.incremental and os.path.exists(OUT_JSON):
        # 增量模式：以 history_data.json 为准（最稳，不依赖断点文件），只补齐缺失项
        try:
            with open(OUT_JSON, "r", encoding="utf-8") as f:
                done = json.load(f)
            print(f"增量模式：已载入 {len(done)} 个组合，仅补齐缺失/新增项")
        except Exception:
            done = {}
    elif args.resume and os.path.exists(PROGRESS):
        try:
            with open(PROGRESS, "r", encoding="utf-8") as f:
                done = json.load(f)
            print(f"读取断点：已完成 {len(done)} 个")
        except Exception:
            done = {}

    log_fh = open(LOG_FILE, "a", encoding="utf-8")
    def log(msg=""):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        log_fh.write(line + "\n"); log_fh.flush()

    speed = "turbo" if args.turbo else "normal"
    c = HistoryCrawler(speed=speed, headless=True, use_cdp=False)
    c.log = log
    c.start()
    t0 = time.time()
    try:
        results = {}      # key -> [(date,price)]
        failed = []
        for i, (name, spec, origin) in enumerate(combos, 1):
            key = f"{name}|{spec}|{origin}"
            if key in done and done[key]:
                results[key] = done[key]
                continue
            pts, err = c.fetch_history(name, spec, origin)
            # 别名回退（只换品名，规格产地沿用）
            if not pts and name in alias_map:
                for alt in alias_map[name]:
                    pts, err = c.fetch_history(alt, spec, origin)
                    if pts:
                        log(f"  🔄 {name} 用别名「{alt}」取到 {len(pts)} 点")
                        break
            if pts:
                results[key] = pts
                done[key] = pts
            else:
                failed.append(f"{key} ({err})")
                done[key] = []
                log(f"  ⚠️ {key} 无历史数据：{err}")
            if i % 10 == 0 or i == len(combos):
                el = time.time() - t0
                rate = i / el if el else 0
                log(f"  进度 {i}/{len(combos)} | {rate:.2f}/s | "
                    f"成功 {len(results)} 失败 {len(failed)} | 已用 {el/60:.1f}min")
                with open(PROGRESS, "w", encoding="utf-8") as f:
                    json.dump(done, f, ensure_ascii=False)
            time.sleep(c.med_gap)
    finally:
        c.stop()
        with open(PROGRESS, "w", encoding="utf-8") as f:
            json.dump(done, f, ensure_ascii=False)
        log_fh.close()

    # ---- 写出文件 ----
    # 长格式 CSV
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["品名", "规格", "产地", "日期", "价格"])
        for key, pts in results.items():
            name, spec, origin = key.split("|", 2)
            for dt, price in pts:
                w.writerow([name, spec, origin, dt, price])
    # JSON 嵌套
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    total_pts = sum(len(v) for v in results.values())
    print(f"\n完成：{len(results)} 个组合有数据，{len(failed)} 个无数据，"
          f"共 {total_pts} 个价格点")
    print(f"已写出：\n  {OUT_CSV}\n  {OUT_JSON}")
    if failed:
        print("无数据组合：")
        for x in failed[:20]:
            print("  -", x)


if __name__ == "__main__":
    main()
