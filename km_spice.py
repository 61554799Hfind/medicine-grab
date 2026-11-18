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
    4) 月价格    POST /jiage/resouces/jsp/price_history_list.jsp
       data: name/standard/origin（**要 encodeURI 两次**）→ JSON {"price":"135.00","datalist":[{"year","month","price"},...]}
       就是网页「月价格」那条曲线 + 下面的历史月均价表，实测每味 35 个月；
       明细表「历史价格」列点进去看的就是这个。

香料范围：
    以 medicine_list.txt（药通网那份 971 味的清单）为准，见下方 SPICE_GROUPS：
      辛香调味（八角茴香/桂皮/干姜/胡椒/豆蔻/砂仁/丁香/花椒/孜然/山奈/姜黄/
                荜澄茄/甘松/檀香/沉香/益智/芥子/香菜籽 …）、
      药食同源花草（紫苏/薄荷/藿香/桂花/玫瑰/茉莉/柠檬/橘红）、
      沿用原口径药材（白芷/陈皮/甘草）。
    别名：康美把「山奈」写成「山柰」，用 SPICE_ALIAS 映射回药通网写法。
    排除：品名以 油/脑/粉/膏 结尾的提取物，以及八角枫这类外形相似但不是香料的药材。
    匹配方式：康美一行的「品名+规格」里只要出现某个香料名就算命中
    （康美把细分类写在规格里：品名=胡椒、规格=白胡椒统）。
    实测：康美今日报价 1104 行 / 683 个品名，药通网 971 味里有 608 味能对上；
    香料按上述口径命中约 80+ 行（旧口径只有 46 行 / 27 个品名）。

产出：
    km_spice_price.csv    明细，一行 = 品名/规格/产地 + 六市场价 + 涨跌
    km_spice_hist.json    走势/月价，{"品名|规格|产地": {
                              "unit": "元/千克",
                              "series":  [["2026-11-17", 28.0], ...],   # 逐日，近一年
                              "monthly": [{"year":2026,"month":11,"price":28.0}, ...]  # 逐月，约 3 年
                          }}

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
URL_MONTH = BASE + "/jiage/resouces/jsp/price_history_list.jsp"

MARKETS = ["亳州", "安国", "成都", "玉林", "廉桥", "普宁"]

# 明细表头（末列固定「更新时间」，带时分，其它数据线是「更新日期」只有日期）
# 「历史价格」是空列：推送时由 km_spice_push.py 填成超链接，点开就是这味的月价格曲线
PRICE_HEADER = (["品名", "规格", "产地"] + MARKETS +
                ["现价", "最高价", "最低价", "六市场均价",
                 "周涨跌", "月涨跌", "季涨跌", "年涨跌", "走势",
                 "历史价格", "更新时间"])

# 香料口径：名字全部来自药通网 medicine_list.txt（971 味），按三组收
#   · 辛香调味 —— 真正意义上的香辛料
#   · 药食同源花草 —— 食品调味/代茶也用，一并收
#   · 沿用原口径药材 —— 第一批就在表里的白芷/陈皮/甘草，继续留着，别让历史数据断档
# 别名说明：康美把「山奈」写成「山柰」，只加别名，不改标准名。
SPICE_GROUPS = [
    ("辛香调味", [
        "八角茴香", "桂皮", "肉桂", "桂枝", "草果", "草果仁", "干姜", "高良姜",
        "姜黄", "片姜黄", "胡椒", "白胡椒", "黑胡椒", "小茴香", "孜然", "香菜籽",
        "荜茇", "荜澄茄", "山奈", "花椒", "香叶", "香茅草", "排草", "甘松",
        "芥子", "胡芦巴", "香砂", "白豆蔻", "草豆蔻", "红豆蔻", "肉豆蔻",
        "砂仁", "丁香", "公丁香", "母丁香", "檀香", "沉香", "木香", "益智",
    ]),
    ("药食同源花草", [
        "紫苏叶", "紫苏子", "薄荷", "藿香", "广藿香", "桂花", "玫瑰花", "茉莉花",
        "柠檬", "橘红",
    ]),
    ("沿用原口径药材", ["白芷", "陈皮", "甘草"]),
]

# 别名 → 标准名（标准名一律用药通网的写法）
SPICE_ALIAS = {"山柰": "山奈"}

# 品名以这些结尾的是提取物/加工品，不是药材本体，别混进价格表
SKIP_SUFFIX = ("油", "脑", "粉", "膏")


def spice_names():
    """全部标准名（去重、保序）"""
    out = []
    for _, names in SPICE_GROUPS:
        for n in names:
            if n not in out:
                out.append(n)
    return out


def _alias_map():
    """别名 → 标准名，长的优先，保证「白胡椒」先于「胡椒」、「八角茴香」先于任何短词"""
    m = {}
    for _, names in SPICE_GROUPS:
        for n in names:
            m[n] = n
    for a, std in SPICE_ALIAS.items():
        m[a] = std
    return dict(sorted(m.items(), key=lambda kv: -len(kv[0])))


SPICE_NAMES = spice_names()

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


# ------------------------------------------------------------------ 香料挑选
def pick_spice(rows):
    """从全量价格里挑出香料行，命中哪个标准名写在 _spice 上

    匹配是对「品名 + 规格」做包含判断：康美把细分写在规格里（品名=胡椒、规格=白胡椒统），
    所以「白胡椒 / 黑胡椒」都能落回「胡椒」这一行，不会重复计数。
    """
    amap = _alias_map()
    kept = []
    for r in rows:
        if r["品名"].endswith(SKIP_SUFFIX):
            continue
        blob = r["品名"] + r["规格"]
        hit = next((std for alias, std in amap.items() if alias in blob), "")
        if hit:
            r["_spice"] = hit
            kept.append(r)
    return kept


# ------------------------------------------------------------------ 主流程
def crawl(max_pages=None, gap=0.15, series=True, monthly=True):
    """抓一遍，返回 (price_rows, hist_dict, stats)

    hist_dict 里每味两个字段：
        series  —— 逐日（近一年，364 个点），走势页的折线图用这个
        monthly —— 逐月（约 35 个月），「月价格」那一列（= 网页月价格曲线最后一格）用这个
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
    picked = pick_spice(prices)
    log("  其中香料 %d 行 / %d 个药通网名" % (len(picked), len({r["_spice"] for r in picked})))

    stamp = time.strftime("%Y-%m-%d %H:%M")      # 抓取时刻（北京时间，带时分）
    hist, no_hist, no_month = {}, [], []
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
        r["更新时间"] = stamp

        key = "%s|%s|%s" % (r["品名"], r["规格"], r["产地"])
        rec = hist.setdefault(key, {"unit": "元/千克"})
        if series:
            try:
                pts = k.history(r["品名"], r["规格"], r["产地"])
            except Exception as e:      # 单味失败不拖死整批，记下来继续
                log("    走势失败 %s：%s" % (key, e))
                pts = []
            if pts:
                rec["series"] = pts
            else:
                no_hist.append(key)
            time.sleep(gap)
        if monthly:
            try:
                ms = k.months(r["品名"], r["规格"], r["产地"])
            except Exception as e:
                log("    月价失败 %s：%s" % (key, e))
                ms = []
            if ms:
                rec["monthly"] = ms
            else:
                no_month.append(key)
            time.sleep(gap)
        if i % 10 == 0:
            log("    走势/月价 %d/%d" % (i, len(picked)))

    stats = {"prices": len(prices), "ranks": len(ranks), "spice_rows": len(picked),
             "spice_names": len({r["_spice"] for r in picked}),
             "with_hist": sum(1 for v in hist.values() if v.get("series")),
             "with_month": sum(1 for v in hist.values() if v.get("monthly")),
             "no_hist": no_hist, "no_month": no_month}
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


def run(max_pages=None, series=True, monthly=True):
    rows, hist, stats = crawl(max_pages=max_pages, series=series, monthly=monthly)
    write_files(rows, hist)
    return stats


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-pages", type=int, default=None,
                    help="每类接口只翻前 N 页（测试用）")
    ap.add_argument("--no-series", action="store_true", help="不抓历史走势（逐日）")
    ap.add_argument("--no-monthly", action="store_true", help="不抓月价格（逐月）")
    a = ap.parse_args()
    s = run(max_pages=a.max_pages, series=not a.no_series,
            monthly=not a.no_monthly)
    print(json.dumps(s, ensure_ascii=False, indent=1))
