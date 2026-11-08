# -*- coding: utf-8 -*-
r"""
药通网「供货信息」抓取 —— 供应商数据线（一品多供应商）

接口（实测 2026-10-29）
------------------------------------------------------------------
    GET /gongqiu/queryGQ!getList.do
        totalCount=33806  gqflg=0  ycnam=<品名>  chandi=
        guige=  order_by=  pageIndex=0  pageSize=200

    · gqflg = 0 供货（供应商），1 求购
    · 返回 JSON：{total, curPage, data:[...], totalPage, pageSize, state}
    · ⚠️ total / totalPage 是**回显我传的 totalCount**，不是真实条数，
      所以必须自己翻页，翻到 data 为空为止。
    · 单个品名 pageSize=200 一页，黄芪 206 条（2 页）、板蓝根 121 条（1 页）。

每条记录的有效字段
------------------------------------------------------------------
    ycnam    品名          guige   规格          chandi   产地
    huoyuandi 货源所在地    pri     单价          shul     数量
    unam     联系人        mob     手机
    addr     供货说明（"指甲片、投料、一手货源"）
    dtm      发布日期      shengyuts 剩余天数
    personrenzheng 实名认证（有 img 标签 = 已认证）
    gqid     供货编号（唯一，用于去重）

⚠️ 本机必须 trust_env=False
------------------------------------------------------------------
用户开着 VPN 时系统代理会让药通网请求超时（实测 15s 无响应），
关掉环境代理后直连 0.13 秒返回。所以 session 一律 trust_env=False。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe supplier_crawl.py --limit 30        # 先试 30 味
    .venv\Scripts\python.exe supplier_crawl.py                   # 全量
    .venv\Scripts\python.exe supplier_crawl.py --names 黄芪,党参  # 指定品名
    .venv\Scripts\python.exe supplier_crawl.py --resume          # 断点续跑
"""
import argparse
import csv
import json
import os
import random
import re
import string
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SEED_CSV = os.path.join(BASE_DIR, "药材涨落对比结果.csv")
MEDICINE_FILE = os.path.join(BASE_DIR, "medicine_list.txt")   # 971 味全集
OUT_CSV = os.path.join(BASE_DIR, "supplier_data.csv")
CACHE_JSON = os.path.join(BASE_DIR, "supplier_data.json")
FAILED_TXT = os.path.join(BASE_DIR, "supplier_failed.txt")

HOST = "https://www.yt1998.com"
LIST_URL = HOST + "/gongqiu/queryGQ!getList.do"
ENTRY_URL = HOST + "/supplyInfo.html"
PASS_COOKIE = "zshcookiename"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

PAGE_SIZE = 200
TOTAL_HINT = 33806          # 全站供货总数，仅作为 totalCount 回显用
MAX_PAGES = 40              # 单品种最多翻 40 页（8000 条），防死循环
MAX_AGE_DAYS = 730          # 只保留近 2 年的发布记录（更老的报价已无参考价值）
BASE_GAP = 0.18             # 请求间隔，实测 0.15~0.3 都安全
MAX_RETRY = 4

HEADER = ["更新时间", "品名", "规格", "产地", "货源所在地", "单价",
          "数量", "联系人", "手机", "供货说明", "发布日期", "剩余天数", "实名认证"]

TAG_RE = re.compile(r"<[^>]+>")


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean(s):
    """去掉 HTML 标签、转义、空白。"""
    if s is None:
        return ""
    s = TAG_RE.sub("", str(s))
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">"))
    return " ".join(s.split()).strip()


class SupplierCrawler:
    def __init__(self, gap=BASE_GAP):
        self.gap = gap
        self.sess = requests.Session()
        # ⚠️ 绝不能读系统代理：VPN 开着时系统代理会让药通网超时
        self.sess.trust_env = False
        self.sess.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": ENTRY_URL,
        })
        self.stats = {"req": 0, "block": 0, "ok": 0}

    def warmup(self):
        """访问入口页拿会话 cookie，再注入 WAF 通行证。"""
        self.sess.cookies.set(PASS_COOKIE, "".join(
            random.choice(string.ascii_lowercase + string.digits) for _ in range(8)),
            domain="www.yt1998.com")
        r = self.sess.get(ENTRY_URL, timeout=25)
        log(f"入口页 {r.status_code} {len(r.text)} 字节；cookie={list(self.sess.cookies.keys())}")

    def _get(self, params):
        """带自适应限速与重试的单页请求。"""
        last = None
        for attempt in range(MAX_RETRY):
            try:
                time.sleep(self.gap)
                t = time.time()
                r = self.sess.get(LIST_URL, params=params, timeout=30)
                self.stats["req"] += 1
                if r.status_code != 200 or not r.text.strip():
                    last = f"HTTP {r.status_code} len={len(r.text)}"
                    self.stats["block"] += 1
                    self.gap = min(self.gap * 1.8, 3.0)
                    log(f"  ⏳ {last}，放慢到 {self.gap:.2f}s")
                    continue
                try:
                    j = r.json()
                except ValueError:
                    last = "返回非 JSON（可能被 WAF 拦）"
                    self.stats["block"] += 1
                    self.gap = min(self.gap * 1.8, 3.0)
                    log(f"  ⏳ {last}，放慢到 {self.gap:.2f}s")
                    continue
                # 成功一次就慢慢提速回去
                self.gap = max(BASE_GAP, self.gap * 0.95)
                self.stats["ok"] += 1
                return j
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                self.stats["block"] += 1
                log(f"  ⏳ {last}，重试 {attempt + 1}/{MAX_RETRY}")
                time.sleep(2 + attempt * 2)
        raise RuntimeError(f"请求失败：{last}")

    def fetch_herb(self, name):
        """抓一个品名的全部供货记录（自动翻页）。"""
        out, seen = [], set()
        for pi in range(MAX_PAGES):
            j = self._get({
                "totalCount": TOTAL_HINT, "gqflg": "0",
                "ycnam": name, "chandi": "", "guige": "", "order_by": "",
                "pageIndex": pi, "pageSize": PAGE_SIZE,
            })
            data = j.get("data") or []
            if not data:
                break
            for d in data:
                gqid = str(d.get("gqid") or "").strip()
                # 接口偶尔会把相似品名也带出来，只保留精确匹配
                if clean(d.get("ycnam")) != name:
                    continue
                key = gqid or f"{d.get('mob')}|{d.get('dtm')}|{d.get('addr')}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(d)
            if len(data) < PAGE_SIZE:
                break
        return out


def load_names(limit=None, only=None):
    """品名清单：优先 --names，否则合并 medicine_list.txt + 种子 CSV（去重保序）。

    medicine_list.txt 是 971 味全集；药材涨落对比结果.csv 可能只是某次试跑的子集，
    所以两份都读，以 medicine_list.txt 的顺序为准，CSV 里多出来的追加在后面。
    """
    if only:
        return [n.strip() for n in only.split(",") if n.strip()]
    names, seen = [], set()
    for path in (MEDICINE_FILE, SEED_CSV):
        if not os.path.exists(path):
            continue
        if path.endswith(".txt"):
            with open(path, encoding="utf-8-sig") as f:
                seq = [l.strip() for l in f if l.strip()]
        else:
            with open(path, encoding="utf-8-sig", newline="") as f:
                seq = [(r.get("品名") or "").strip() for r in csv.DictReader(f)]
        for n in seq:
            if n and n not in seen:
                seen.add(n)
                names.append(n)
    return names[:limit] if limit else names


def to_row(d, today):
    """接口记录 -> 表格一行（列顺序见 HEADER）。"""
    real = "已认证" if clean(d.get("personrenzheng")) or "renzheng" in str(d.get("personrenzheng")) else ""
    return [
        today,
        clean(d.get("ycnam")),
        clean(d.get("guige")),
        clean(d.get("chandi")),
        clean(d.get("huoyuandi")),
        clean(d.get("pri")),
        clean(d.get("shul")),
        clean(d.get("unam")),
        clean(d.get("mob")),
        clean(d.get("addr")),
        clean(d.get("dtm")),
        clean(d.get("shengyuts")),
        real,
    ]


def is_fresh(d, max_age_days=MAX_AGE_DAYS):
    """发布日期在 max_age_days 天内才算有效；日期解析不了就保留。"""
    raw = clean(d.get("dtm"))
    try:
        dt = time.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return True
    age = (time.time() - time.mktime(dt)) / 86400
    return age <= max_age_days


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="只抓前 N 个品名")
    ap.add_argument("--names", help="只抓这些品名，逗号分隔")
    ap.add_argument("--resume", action="store_true", help="从 supplier_data.json 断点续跑")
    ap.add_argument("--gap", type=float, default=BASE_GAP)
    ap.add_argument("--max-age-days", type=int, default=MAX_AGE_DAYS,
                    help="只保留最近 N 天发布的记录，0 = 不过滤")
    args = ap.parse_args()

    names = load_names(args.limit, args.names)
    log(f"待抓品名 {len(names)} 个")

    cache = {}
    if args.resume and os.path.exists(CACHE_JSON):
        with open(CACHE_JSON, encoding="utf-8") as f:
            cache = json.load(f)
        log(f"断点续跑：已有 {len(cache)} 个品名")

    crawler = SupplierCrawler(gap=args.gap)
    crawler.warmup()

    failed = []
    t0 = time.time()
    for i, name in enumerate(names, 1):
        if name in cache:
            continue
        try:
            rows = crawler.fetch_herb(name)
            cache[name] = rows
            log(f"[{i}/{len(names)}] {name} → {len(rows)} 条 "
                f"(累计 {sum(len(v) for v in cache.values())} 条, {time.time()-t0:.0f}s)")
        except Exception as e:
            failed.append(name)
            log(f"[{i}/{len(names)}] ❌ {name} 失败：{e}")
        # 每 20 个品名落一次盘，防止中途崩掉全丢
        if i % 20 == 0:
            with open(CACHE_JSON, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)

    with open(CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)

    today = time.strftime("%Y-%m-%d")
    total, dropped = 0, 0
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for name in names:
            recs = cache.get(name, [])
            if args.max_age_days:
                kept = [d for d in recs if is_fresh(d, args.max_age_days)]
                dropped += len(recs) - len(kept)
                recs = kept
            # 新发布的排前面，老板打开就看到有效供应商
            recs.sort(key=lambda d: clean(d.get("dtm")), reverse=True)
            for d in recs:
                w.writerow(to_row(d, today))
                total += 1
    if failed:
        with open(FAILED_TXT, "w", encoding="utf-8") as f:
            f.write("\n".join(failed))
    log(f"完成：{len(cache)} 个品名 / {total} 行"
        + (f"（过滤掉 {dropped} 条超期记录）" if dropped else "")
        + f" → {OUT_CSV}")
    log(f"请求 {crawler.stats['req']} 次，被挡 {crawler.stats['block']} 次，"
        f"耗时 {time.time()-t0:.0f}s")
    if failed:
        log(f"失败 {len(failed)} 个，见 {FAILED_TXT}")


if __name__ == "__main__":
    main()
