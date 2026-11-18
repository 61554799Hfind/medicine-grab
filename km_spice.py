# -*- coding: utf-8 -*-
r"""
康美中药网 kmzyw.com.cn「药材价格 + 价格起伏」抓取（零第三方依赖）

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
    4) 月价格    POST /jiage/resouces/jsp/price_history_list.jsp
       data: name/standard/origin（**要 encodeURI 两次**）→ JSON {"price":"135.00","datalist":[{"year","month","price"},...]}
       就是网页「月价格」那条曲线 + 下面的历史月均价表，实测每味 35 个月；
       明细表「历史价格」列点进去看的就是这个。

收录范围（2026-11-18 用户明确：按 medicine_list.txt 对照，不按「是不是香料」判断）：
    以 medicine_list.txt（药通网那份 971 味）为准，**康美品名逐字命中清单就收**。
    实测：康美今日报价 1104 行 / 683 个品名，其中 580 个品名在清单里 = 980 行；
    再加 NAME_ALIAS 里 9 个「同物异名」写法（山柰/公丁香/草果仁/薄荷叶/桑椹/
    灯心草/海浮石/墨旱莲/桂圆）→ 589 个品名 / 991 行。
    清单里康美没有报价的 391 味，自然进不来（平台没有这味）。
    康美有、清单里没有的 103 个品名（壁虎/燕窝/鱼鳔/五谷虫/桃胶…）不收，因为对比基准是清单。

产出：
    km_spice_price.csv    明细，一行 = 品名/规格/产地 + 六市场价 + 涨跌
    km_spice_hist.json    月价格 + 走势，{"品名|规格|产地": {
                              "unit": "元/千克",
                              "monthly": [{"year":2026,"month":11,"price":28.0}, ...],  # 逐月，约 3 年
                              "series":  [["2026-11-17", 28.0], ...]                   # 逐日，近一年（可选）
                          }}

用法：
    python km_spice.py                  # 全量抓，写两个文件
    python km_spice.py --max-pages 2    # 每类只翻 2 页（测试）
    python km_spice.py --series         # 额外抓逐日走势（默认不抓，走势页只用月价格）
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://www.kmzyw.com.cn"
URL_PRICE = BASE + "/jiage/today_price.html"
URL_RANK = BASE + "/jiage/price_ranking.html"
URL_SERIES = BASE + "/jiage/resouces/jsp/price_history_todaylist.jsp"
URL_MONTH = BASE + "/jiage/resouces/jsp/price_history_list.jsp"

MARKETS = ["亳州", "安国", "成都", "玉林", "廉桥", "普宁"]

# 明细表头（末列固定「更新时间」，带时分，其它数据线是「更新日期」只有日期）
# 「历史价格」是空列：推送时由 km_spice_push.py 填成超链接，点开就是这味的月价格曲线
PRICE_HEADER = (["品名", "规格", "产地"] + MARKETS +
                ["现价", "最高价", "最低价", "六市场均价",
                 "周涨跌", "月涨跌", "季涨跌", "年涨跌", "走势",
                 "历史价格", "更新时间"])

# 收录口径：以 medicine_list.txt（药通网 971 味）为准，品名逐字命中就收
MED_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicine_list.txt")

# 同物异名：康美的叫法 → medicine_list 里的写法（只收「同一个东西换个写法/叫法」的，
# 像粉葛/广山药/关黄柏那种其实是另一个物种或另一个炮制规格的，宁可漏也不混进来）
NAME_ALIAS = {
    "山柰": "山奈",      # 康美用「柰」，药通网用「奈」
    "公丁香": "丁香",
    "草果仁": "草果",
    "薄荷叶": "薄荷",
    "桑椹": "桑葚",
    "灯心草": "灯芯草",
    "海浮石": "浮海石",
    "墨旱莲": "旱莲草",
    "桂圆": "龙眼肉",
}

# 品名以这些结尾的是提取物/加工品，不是药材本体，别混进价格表
# （收录口径改成「按清单对照」后不再按后缀过滤，变量留着是给人工核对用）
SKIP_SUFFIX = ("油", "脑", "粉", "膏")

_MED_CACHE = None


def med_names():
    """medicine_list.txt 里的 971 味（去重、保序）"""
    global _MED_CACHE
    if _MED_CACHE is None:
        if not os.path.exists(MED_LIST_FILE):
            raise SystemExit("找不到 %s —— 收录口径靠它，缺了就没法判断收哪些" % MED_LIST_FILE)
        txt = open(MED_LIST_FILE, encoding="utf-8-sig").read()
        names = []
        for line in txt.splitlines():
            n = line.strip()
            if n and n not in names:
                names.append(n)
        _MED_CACHE = names
    return _MED_CACHE


def name_map():
    """康美品名 → 药通网写法（只有清单里的名字才会出现在这个字典里）"""
    names = set(med_names())
    m = {n: n for n in names}
    for km, std in NAME_ALIAS.items():
        if std in names:            # 别把清单里根本没有的名字放进来
            m[km] = std
    return m


MED_MAP = None

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


class KmHttpError(RuntimeError):
    """带 URL 和状态码的 HTTP 错误 —— 函数日志里一眼能看出是哪个接口挂了"""

    def __init__(self, method, url, code):
        self.method, self.url, self.code = method, url, code
        RuntimeError.__init__(self, "%s %s → HTTP %d" % (method, url, code))


class Kmzyw:
    """康美中药网的三个接口。一个实例复用一个会话（会拿 cookie）。"""

    def __init__(self):
        cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.op.addheaders = [("User-Agent", UA), ("Referer", BASE + "/jiage/"),
                              ("Accept-Language", "zh-CN,zh;q=0.9")]

    # ---------------- 底层 ----------------
    def _open(self, url, data=None, timeout=40, retries=5):
        """翻页翻快了平台会回 502（限流），退避重试；4xx 直接抛，别浪费时间。

        所有失败都带上「方法 + 完整 URL + 状态码」，函数报错时能直接看出是哪个接口。
        """
        method = "POST" if data else "GET"
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
                    raise KmHttpError(method, url, e.code) from None
                wait = 6 * (attempt + 1)
                log("    HTTP %d，等 %ds 重试（%d/%d）" % (e.code, wait, attempt + 1, retries))
                time.sleep(wait)
            except Exception as e:
                if attempt == retries - 1:
                    raise RuntimeError("%s %s 失败：%s: %s"
                                       % (method, url, type(e).__name__, e)) from None
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
            try:
                html = self._open("%s?pageNum=%d" % (url, page))
            except KmHttpError as e:
                if e.code in (400, 404):            # 页码越界：当成翻到末尾，别整个任务挂掉
                    log("    第 %d 页 HTTP %d，视为末尾" % (page, e.code))
                    break
                raise
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

    def months(self, name, standard, origin):
        """月价格（就是网页「月价格」那条曲线）：返回 [{"year","month","price"}, ...]

        接口同上但换成 price_history_list.jsp，参数要**编码两次**
        （页面里是 encodeURI(encodeURI(x))，只编一次会回「暂无此药材数据」）。
        实测每味回 35 个月（约 3 年），含当年逐月均价；没有月价格的只占极少数（檀香白/黄、沉香含量15）。
        """
        q = lambda s: urllib.parse.quote(urllib.parse.quote(s))
        body = urllib.parse.urlencode({
            "name": q(name), "standard": q(standard), "origin": q(origin),
        }).encode()
        txt = self._open(URL_MONTH, data=body, timeout=40, retries=3).strip()
        if not txt:
            return []
        i = txt.find("{")
        if i < 0:
            return []
        try:
            j = json.loads(txt[i:])
        except ValueError:
            return []
        if not j.get("success"):
            return []
        out = []
        for d in j.get("datalist") or []:
            try:
                out.append({"year": int(d["year"]), "month": int(d["month"]),
                            "price": round(float(d["price"]), 2)})
            except (KeyError, TypeError, ValueError):
                continue
        out.sort(key=lambda x: (x["year"], x["month"]))
        return out


# ------------------------------------------------------------------ 按清单挑选
def pick_herb(rows):
    """从康美全量报价里挑出「medicine_list.txt 里有这一味」的行

    判断是**逐字命中清单**（不是「像不像香料」）：康美品名 == 清单里的名字，
    或命中 NAME_ALIAS 里的同物异名。规格不参与判断，所以「胡椒/白胡椒统」
    「胡椒/黑胡椒统」都算「胡椒」这一味的两个规格，各占一行。
    """
    global MED_MAP
    if MED_MAP is None:
        MED_MAP = name_map()
    kept = []
    for r in rows:
        std = MED_MAP.get(r["品名"])
        if not std:
            continue
        r["_std"] = std
        kept.append(r)
    return kept


# ------------------------------------------------------------------ 主流程
_TL = threading.local()


def _thread_client():
    """每个线程一个会话（cookie 各自持有），避免共享 opener 抢连接"""
    k = getattr(_TL, "k", None)
    if k is None:
        k = Kmzyw()
        try:
            k.warmup()
        except Exception:
            pass
        _TL.k = k
    return k


def _fetch_month(item):
    name, standard, origin = item
    return _thread_client().months(name, standard, origin)


def crawl(max_pages=None, gap=0.15, series=False, monthly=True, workers=6):
    """抓一遍，返回 (price_rows, hist_dict, stats)

    hist_dict 里每味两个字段：
        monthly —— 逐月（约 35 个月），走势页的图 + 历史月均价表都用这个
        series  —— 逐日（近一年，364 个点），只有显式传 series=True 才抓（走势页不用）

    月价格是**并发**抓的：991 行串行要 20 分钟（单请求 1.45s），6 并发实测约 4 分钟，
    仍在上限 600 秒之内。翻页保持串行，翻了快了平台会 502。
    """
    k = Kmzyw()
    k.warmup()

    log("抓 市场价格 ...")
    prices = k.price_list(max_pages)
    log("  市场价 %d 行" % len(prices))

    log("抓 价格涨跌 ...")
    ranks = k.rank_list(max_pages)
    log("  涨跌 %d 行" % len(ranks))

    rank_map = {r["key"]: r for r in ranks}
    picked = pick_herb(prices)
    log("  其中清单内 %d 行 / %d 个品名（medicine_list %d 味）"
        % (len(picked), len({r["品名"] for r in picked}), len(med_names())))

    stamp = time.strftime("%Y-%m-%d %H:%M")      # 抓取时刻（北京时间，带时分）
    hist, no_hist, no_month = {}, [], []

    for r in picked:                              # 先把涨跌、均价这些本地的算完
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
        r["更新时间"] = stamp

    def key_of(r):
        return "%s|%s|%s" % (r["品名"], r["规格"], r["产地"])

    if monthly:
        items = [(r["品名"], r["规格"], r["产地"]) for r in picked]
        log("抓 月价格（%d 味，%d 并发）..." % (len(items), workers))
        done = [0]
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_month, it): it for it in items}
            for fut in as_completed(futs):
                it = futs[fut]
                key = "%s|%s|%s" % it
                rec = hist.setdefault(key, {"unit": "元/千克"})
                try:
                    ms = fut.result()
                except Exception as e:            # 单味失败不拖死整批
                    log("    月价失败 %s：%s" % (key, e))
                    ms = []
                if ms:
                    rec["monthly"] = ms
                else:
                    no_month.append(key)
                done[0] += 1
                if done[0] % 100 == 0:
                    log("    月价 %d/%d" % (done[0], len(items)))

    if series:                                    # 逐日走势：默认不抓（走势页只用月价格）
        log("抓 逐日走势（%d 味）..." % len(picked))
        for i, r in enumerate(picked, 1):
            key = key_of(r)
            rec = hist.setdefault(key, {"unit": "元/千克"})
            try:
                pts = k.history(r["品名"], r["规格"], r["产地"])
            except Exception as e:
                log("    走势失败 %s：%s" % (key, e))
                pts = []
            if pts:
                rec["series"] = pts
            else:
                no_hist.append(key)
            time.sleep(gap)
            if i % 50 == 0:
                log("    走势 %d/%d" % (i, len(picked)))

    stats = {"prices": len(prices), "ranks": len(ranks), "herb_rows": len(picked),
             "herb_names": len({r["品名"] for r in picked}),
             "med_list": len(med_names()),
             "with_month": sum(1 for v in hist.values() if v.get("monthly")),
             "with_hist": sum(1 for v in hist.values() if v.get("series")),
             "no_month": no_month, "no_hist": no_hist}
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


def run(max_pages=None, series=False, monthly=True, workers=6):
    rows, hist, stats = crawl(max_pages=max_pages, series=series,
                              monthly=monthly, workers=workers)
    write_files(rows, hist)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None,
                    help="每类接口只翻前 N 页（测试用）")
    ap.add_argument("--series", action="store_true",
                    help="额外抓逐日走势（走势页只用月价格，一般不用开）")
    ap.add_argument("--no-monthly", action="store_true", help="不抓月价格（逐月）")
    ap.add_argument("--workers", type=int, default=6, help="月价格并发数，默认 6")
    a = ap.parse_args()
    s = run(max_pages=a.max_pages, series=a.series,
            monthly=not a.no_monthly, workers=a.workers)
    print(json.dumps(s, ensure_ascii=False, indent=1))
