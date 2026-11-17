# -*- coding: utf-8 -*-
r"""
康美中药网 kmzyw.com.cn「香料价格 + 价格起伏」抓取（零第三方依赖）

为什么要单独一个域名：
    康美有两套系统。抓供求用的 www.kmb2b.com 上，价格页全部 302 跳登录页；
    价格数据在 www.kmzyw.com.cn（康美中药网）上免费公开，不用登录。

    这个域名的境外访问实测不通（GitHub 海外 runner 三个请求全部 30 秒超时），
    所以和 kmb2b 一样只能由阿里云函数（国内出口）抓，抓完提交 CSV/JSON 回仓库，
    再由 GitHub Actions 拿去推 Google 表格。

数据来源（三个接口，都免登录）：
    1) 市场价格  GET  /jiage/today_price.html?pageNum=N
       56 页 × 20 条 = 1104 行，一行 = 品名/规格/产地 + 亳州 安国 成都 玉林 廉桥 普宁 六个市场价
    2) 价格涨跌  GET  /jiage/price_ranking.html?pageNum=N
       55 页，一行 = 品名/规格/产地 + 价格 + 周涨跌 月涨跌 季涨跌 年涨跌（7/30/90/365 日）
    3) 历史走势  POST /jiage/resouces/jsp/price_history_todaylist.jsp
       data: name/standard/origin/site → JSON {"price":"28.00","data":[[时间戳ms,价格],...]}
       实测每味回 364 个点（近一年逐日），site 传哪个市场结果都一样，是平台代表性报价。

香料范围：
    以 medicine_list.txt（药通网那份 971 味的清单）为准，取出其中的香料名
    （见下方 SPICE_NAMES，由 桂皮/肉桂/八角茴香/草果/干姜/胡椒/茴香/丁香/花椒/香叶/
      白芷/陈皮/甘草/豆蔻/砂仁/山柰/良姜/香茅/排草/孜然/荜茇/香砂 这些关键词挑出来，
      并去掉八角莲、八角金盘、八角枫、山甘草这类明显不是香料的药材）。
    匹配方式：康美一行的「品名+规格」里只要出现某个香料名就算命中
    （康美把细分类写在规格里：品名=胡椒、规格=白胡椒统）。

产出：
    km_spice_price.csv    明细，一行 = 品名/规格/产地 + 六市场价 + 涨跌
    km_spice_hist.json    走势，{“品名|规格|产地”: {"unit": "元/千克", "series": [["2026-11-17", 28.0], ...]}}

用法：
    python km_spice.py                  # 全量抓，写两个文件
    python km_spice.py --max-pages 2    # 每类只翻 2 页（测试）
"""

import argparse
import csv
import datetime
import gzip
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://www.kmzyw.com.cn"
URL_PRICE = BASE + "/jiage/today_price.html"
URL_RANK = BASE + "/jiage/price_ranking.html"
URL_SERIES = BASE + "/jiage/resouces/jsp/price_history_todaylist.jsp"

MARKETS = ["亳州", "安国", "成都", "玉林", "廉桥", "普宁"]

# 明细表头（末列固定「更新日期」，与其它数据线保持一致）
PRICE_HEADER = (["品名", "规格", "产地"] + MARKETS +
                ["现价", "最高价", "最低价", "六市场均价",
                 "周涨跌", "月涨跌", "季涨跌", "年涨跌", "走势", "更新日期"])

# 药通网 medicine_list.txt 里的香料口径（去掉明显非香料的八角莲/八角金盘/八角枫/山甘草/紫豆蔻）
SPICE_NAMES = [
    "八角茴香", "桂皮", "肉桂", "桂枝", "草果", "干姜",
    "胡椒", "白胡椒", "黑胡椒", "小茴香", "丁香", "母丁香", "公丁香",
    "花椒", "香叶", "白芷", "陈皮", "甘草",
    "白豆蔻", "草豆蔻", "红豆蔻", "肉豆蔻", "砂仁",
    "高良姜", "香茅草", "排草", "孜然", "荜茇", "香砂",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

_TR = re.compile(r"<tr\s+drugid='([\w\-]+)'>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[\s\u00a0\u3000]+")
_TOTAL = re.compile(r"共\s*(\d+)\s*页")

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))


def log(msg=""):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def clean(s):
    return _WS.sub(" ", _TAG.sub("", s or "")).strip()


def num(s):
    """'9500.00' -> 9500.0；'--' / '' -> None"""
    s = (s or "").strip()
    if not s or s in ("--", "-", "—"):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def pct(s):
    """'31.57%' -> 31.57；'-8.33%' -> -8.33；'--' -> None"""
    s = clean(s).replace("%", "").strip()
    return num(s)


class Kmzyw:
    """康美中药网的三个接口。一个实例复用一个会话（会拿 cookie）。"""

    def __init__(self):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.op.addheaders = [("User-Agent", UA), ("Referer", BASE + "/jiage/"),
                              ("Accept-Language", "zh-CN,zh;q=0.9")]

    # ---------------- 底层 ----------------
    def _open(self, url, data=None, timeout=40, retries=5):
        """翻页翻快了平台会回 502（限流），退避重试；4xx 直接抛，别浪费时间。"""
        req = urllib.request.Request(url, data=data)
        if data:
            req.add_header("Content-Type",
                           "application/x-www-form-urlencoded; charset=UTF-8")
            req.add_header("X-Requested-With", "XMLHttpRequest")
        for attempt in range(retries):
            try:
                with self.op.open(req, timeout=timeout) as r:
                    b = r.read()
                    if r.headers.get("Content-Encoding") == "gzip":
                        b = gzip.decompress(b)
                    return b.decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                if e.code < 500 or attempt == retries - 1:
                    raise
                wait = 6 * (attempt + 1)
                log("    HTTP %d，等 %ds 重试（%d/%d）" % (e.code, wait, attempt + 1, retries))
                time.sleep(wait)
            except Exception as e:
                if attempt == retries - 1:
                    raise
                wait = 3 * (attempt + 1)
                log("    %s，等 %ds 重试（%d/%d）"
                    % (type(e).__name__, wait, attempt + 1, retries))
                time.sleep(wait)

    def warmup(self):
        """先访问一次列表页，拿平台会话 cookie。"""
        self._open(URL_PRICE)

    # ---------------- 三个接口 ----------------
    @staticmethod
    def parse_rows(html):
        """把表格解析成 [[单元格文本, ...], ...]（跳过表头）"""
        out = []
        for _, body in _TR.findall(html):
            cells = [clean(c) for c in _TD.findall(body)]
            if cells:
                out.append(cells)
        return out

    def fetch_pages(self, url, max_pages=None, gap=0.35):
        """按 ?pageNum=N 翻页，翻到空页或超过总页数为止"""
        first = self._open(url)
        pages = self.parse_rows(first)
        if not pages:
            return []
        m = _TOTAL.search(first)
        total = int(m.group(1)) if m else 999
        rows = list(pages)
        page = 1
        while page < total and (not max_pages or page < max_pages):
            page += 1
            html = self._open("%s?pageNum=%d" % (url, page))
            got = self.parse_rows(html)
            if not got:
                break
            rows.extend(got)
            if page % 10 == 0:
                log("    第 %d/%d 页，累计 %d 行" % (page, total, len(rows)))
            time.sleep(gap)
        return rows

    def price_list(self, max_pages=None):
        """市场价格：品名/规格/产地/6 市场价（原样保留页面写法，如 28.00；无报价为 -- → 空）"""
        raw = self.fetch_pages(URL_PRICE, max_pages)
        rows = []
        for c in raw:
            if len(c) < 9:
                continue
            rows.append({
                "品名": c[0], "规格": c[1], "产地": c[2],
                **{MARKETS[i]: (c[3 + i] if num(c[3 + i]) is not None else "")
                   for i in range(6)},
            })
        return rows

    def rank_list(self, max_pages=None):
        """价格涨跌：品名/规格/产地/价格 + 周月季年涨跌"""
        raw = self.fetch_pages(URL_RANK, max_pages)
        rows = []
        for c in raw:
            if len(c) < 9:
                continue
            rows.append({
                "key": (c[0], c[1], c[2]),
                "现价": c[3] if num(c[3]) is not None else "",
                "周涨跌": pct(c[4]), "月涨跌": pct(c[5]),
                "季涨跌": pct(c[6]), "年涨跌": pct(c[7]),
            })
        return rows

    def history(self, name, standard, origin, site="亳州"):
        """历史走势：返回 [(date, price), ...]，按日期升序"""
        body = urllib.parse.urlencode({
            "name": urllib.parse.quote(name),
            "standard": urllib.parse.quote(standard),
            "origin": urllib.parse.quote(origin),
            "site": urllib.parse.quote(site),
        }).encode()
        txt = self._open(URL_SERIES, data=body)
        txt = txt.strip()
        if not txt:
            return []
        try:
            j = json.loads(txt)
        except ValueError:
            return []
        pts = []
        for item in j.get("data") or []:
            try:
                ts, price = int(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            d = (datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc)
                 + datetime.timedelta(hours=8))
            pts.append([d.strftime("%Y-%m-%d"), price])
        pts.sort(key=lambda x: x[0])
        return pts


# ------------------------------------------------------------------ 香料挑选
def matcher():
    """按名字长度倒序，保证「白胡椒」优先于「胡椒」"""
    return sorted(SPICE_NAMES, key=len, reverse=True)


def pick_spice(rows):
    """从 1104 行里挑出香料行，并把命中的药通网名写在 _spice 上"""
    names = matcher()
    kept = []
    for r in rows:
        blob = r["品名"] + r["规格"]
        hit = next((n for n in names if n in blob), "")
        if hit:
            r["_spice"] = hit
            kept.append(r)
    return kept


# ------------------------------------------------------------------ 主流程
def crawl(max_pages=None, gap=0.15, series=True):
    """抓一遍，返回 (price_rows, hist_dict, stats)"""
    k = Kmzyw()
    k.warmup()

    log("抓 市场价格 ...")
    prices = k.price_list(max_pages)
    log("  市场价 %d 行" % len(prices))

    log("抓 价格涨跌 ...")
    ranks = k.rank_list(max_pages)
    log("  涨跌 %d 行" % len(ranks))

    rank_map = {r["key"]: r for r in ranks}
    picked = pick_spice(prices)
    log("  其中香料 %d 行 / %d 个药通网名" % (len(picked), len({r["_spice"] for r in picked})))

    today = time.strftime("%Y-%m-%d")
    hist, no_hist = {}, []
    for i, r in enumerate(picked, 1):
        rk = rank_map.get((r["品名"], r["规格"], r["产地"])) or {}
        vals = [v for v in (num(r[m]) for m in MARKETS) if v is not None]
        r["现价"] = rk.get("现价", "")
        r["最高价"] = "%.2f" % max(vals) if vals else ""
        r["最低价"] = "%.2f" % min(vals) if vals else ""
        r["六市场均价"] = "%.2f" % (sum(vals) / len(vals)) if vals else ""
        for k4 in ("周涨跌", "月涨跌", "季涨跌", "年涨跌"):
            r[k4] = rk.get(k4)
        w = r["周涨跌"]
        r["走势"] = "平" if w is None else ("涨" if w > 0 else ("跌" if w < 0 else "平"))
        r["更新日期"] = today

        key = "%s|%s|%s" % (r["品名"], r["规格"], r["产地"])
        if series:
            pts = k.history(r["品名"], r["规格"], r["产地"])
            if pts:
                hist[key] = {"unit": "元/千克", "series": pts}
            else:
                no_hist.append(key)
            if i % 10 == 0:
                log("    走势 %d/%d" % (i, len(picked)))
            time.sleep(gap)

    stats = {"prices": len(prices), "ranks": len(ranks), "spice_rows": len(picked),
             "spice_names": len({r["_spice"] for r in picked}),
             "with_hist": len(hist), "no_hist": no_hist}
    return picked, hist, stats


def write_files(rows, hist, price_path=None, hist_path=None):
    """写两个文件。hist_path 传 False 表示只写 CSV（阿里云函数自己压 gz）。"""
    price_path = price_path or os.path.join(LOCAL_DIR, "km_spice_price.csv")
    hist_path = hist_path or os.path.join(LOCAL_DIR, "km_spice_hist.json")

    rows = sorted(rows, key=lambda r: (r["品名"], r["规格"], r["产地"]))
    with open(price_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=PRICE_HEADER, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            out = dict(r)
            for k in PRICE_HEADER:
                v = out.get(k)
                if v is None:
                    out[k] = ""
            w.writerow(out)
    log("  → %s  %d 行" % (os.path.basename(price_path), len(rows)))

    if hist_path is not False:
        with open(hist_path, "w", encoding="utf-8") as f:
            json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))
        log("  → %s  %d 个品种走势" % (os.path.basename(hist_path), len(hist)))
    return price_path, hist_path


def run(max_pages=None, series=True):
    rows, hist, stats = crawl(max_pages=max_pages, series=series)
    write_files(rows, hist)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None,
                    help="每类接口只翻前 N 页（测试用）")
    ap.add_argument("--no-series", action="store_true", help="不抓历史走势")
    a = ap.parse_args()
    s = run(max_pages=a.max_pages, series=not a.no_series)
    print(json.dumps(s, ensure_ascii=False, indent=1))
