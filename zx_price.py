# -*- coding: utf-8 -*-
r"""
中香网「香料行情」价格抓取 —— 第五个数据源（2026-11-18 新增）

站点：https://www.zhongxiangwang.cn/latest-price  （导航里叫「香料行情 Spice Quotes」）
接口（要 AES-128-CBC + RSA 加密，和求购线同一套，见 purchase_crawl.py 顶部）：
    POST /api/front/encryptController/getEncryptPubkey        -> RSA 公钥
    POST /api/front/skuChangePrice/getSkuPriceExcelList       -> 全量，一次给完
    POST /api/front/skuChangePrice/getSkuPriceViewPage        -> 分页（备用）
    header = secure: true, reqEnabled: Y, Terminal: PC, Version: 1.0.0

实测（2026-11-18）
------------------------------------------------------------------
· 全站只有 **135 行报价**：国内 50 个品类 / 120 行，国外 4 个品类 / 15 行。
  粒度是「品类 + 产地 + 规格」，不是按市场分（中香网只有玉林一个市场，所以没有六市场价）。
· 字段：skuTypeName 品类 / producer 产地 / spec 规格 /
        todayPrice 今日价 / lastdayPrice 前日价 /
        dailyPriceChangeAmount 日涨跌额 / dailyPriceChangePercentage 较前日 /
        monthPriceChangePercentage 较月初 / yearPriceChangePercentage 较年初 /
        skuUnit 单位（元/公斤、元/克、元/个、元/斤…）
· 另一个接口 skuController/getSkuPricePageList（明文 POST，按 skuTypeId）
  分类树 groupByDesc 有 81 个 skuType，但真正有报价的仍是同样 135 行/50 品类 —— 数据源硬上限。
· **收录口径对标 medicine_list.txt（971 味），口径同康美线**：品类逐字命中清单就收，
  再加 NAME_ALIAS 里的同物异名/规格变体映射（八角→八角茴香、干枝八角→八角茴香、
  桂通→肉桂、圆壳砂仁→砂仁、红花椒→花椒、沙姜→山奈、积壳→枳壳…）。
  CSV 多一列「清单名」标出对应关系：清单内 131 行/41 味；清单外 4 品类
  （圆香果、长香果、绿豆蔻、葫芦巴）照留但清单名留空，宁可标「清单外」也不硬并。
· **没有历史价格接口**：getSkuSpecPriceTrend / getSkuSpecPriceTrendGroup 只回涨跌幅分布
  统计（riseCount/stableCount/fallCount），全站 40 个前端 chunk 里没有任何页面调用它们；
  getSkuPriceHistory 之类的猜想路径全部 404；Excel 接口也不吃日期参数。
  → 所以历史价格只能自己每天累积（zx_price_hist.json.gz），
    另外把官方给的「前日价」也记成前一天的点，第一天就有两个点。

产物
------------------------------------------------------------------
    zx_price.csv          明细（135 行 × 13 列，含「清单名」）
    zx_price_hist.json.gz 每行一个逐日价格序列 {"国内|八角|广西|大红碎片": {"2026-11-18": 22.25}}

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe zx_price.py           # 抓 + 累积历史
    .venv\Scripts\python.exe zx_price.py --no-hist # 只出 CSV，不动历史
"""
import argparse
import base64
import csv
import gzip
import json
import os
import random
import string
import time

import requests

try:
    from cryptography.hazmat.primitives import padding as sympad
    from cryptography.hazmat.primitives.asymmetric import padding as asympad
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_OUT = os.path.join(BASE_DIR, "zx_price.csv")
JSON_HIST = os.path.join(BASE_DIR, "zx_price_hist.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

# 列名直接照官网表头：「品类/产地 | 规格 | 今日 | 前日 | 日涨跌 | 较前日 | 较月初 | 较年初 | 单位」
# 多出来的「市场」是官网的「国内/国外」tab；末列「更新时间」是四条线统一的收尾列。
# 「清单名」= 该品类在 medicine_list.txt（971 味）里对应的标准名，对标口径与康美线一致。
HEADER = ["市场", "品类", "清单名", "产地", "规格", "今日价", "前日价", "日涨跌",
          "较前日", "较月初", "较年初", "单位", "更新时间"]

# 收录口径（2026-11-18 用户明确）：**对标 medicine_list.txt，不按「是不是香料」主观判断**。
# 和康美线同一套：逐字命中清单就收；再加下面这些「同物异名 / 规格变体」映射到清单写法。
# 只收「同一个东西换个写法」的；清单里压根没有的（圆香果/长香果/绿豆蔻/葫芦巴）宁可标清单外也不硬并。
MED_LIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "medicine_list.txt")
NAME_ALIAS = {
    "八角": "八角茴香", "益智仁": "益智", "良姜": "高良姜", "荜拨": "荜茇",
    "沙姜": "山奈", "积壳": "枳壳", "千里香": "九里香", "灵香草": "零陵香",
    # 规格变体归并（本质是同一味的切片/等级/产形）
    "干枝八角": "八角茴香", "桂通": "肉桂", "烟桂-8-10公分": "肉桂",
    "圆壳砂仁": "砂仁", "长壳砂仁": "砂仁",
    "红花椒": "花椒", "青花椒": "花椒",
}
_MED = None


def med_names():
    global _MED
    if _MED is None:
        txt = open(MED_LIST_FILE, encoding="utf-8-sig").read()
        names = []
        for line in txt.splitlines():
            n = line.strip()
            if n and n not in names:
                names.append(n)
        _MED = names
    return _MED


def med_map():
    """中香品类 → 清单标准名（未在清单的返回空串）"""
    names = set(med_names())
    m = {n: n for n in names}
    for zx, std in NAME_ALIAS.items():
        if std in names:
            m[zx] = std
    return m


def in_list(name):
    """给一行报价算清单标准名；不在清单返回 ''"""
    return med_map().get(name, "")

MARKET = {"china": "国内", "foreign": "国外"}
FORBIDDEN = "/\\?%*:|\"<>"          # 工作表名里不能出现的字符


def log(msg=""):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class ZxQuote:
    """中香网价格接口客户端（加密方式与 purchase_crawl.ZxPurchase 相同）"""

    HOST = "https://www.zhongxiangwang.cn"
    PAGE = HOST + "/latest-price"
    PUBKEY = HOST + "/api/front/encryptController/getEncryptPubkey"
    API = HOST + "/api/front/skuChangePrice/"
    IV = b"0392039203920300"

    def __init__(self):
        self.sess = requests.Session()
        # 国内站：绝不能读系统代理（用户开 VPN 时会超时）
        self.sess.trust_env = False
        self.sess.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                                  "Terminal": "PC", "Version": "1.0.0",
                                  "Origin": self.HOST})
        self.key = None
        self.pub = None

    def handshake(self):
        if not HAS_CRYPTO:
            raise RuntimeError("中香网接口要加密，请先 pip install cryptography")
        self.sess.get(self.PAGE, timeout=25)
        j = self.sess.post(self.PUBKEY, json={}, timeout=30,
                           headers={"Content-Type": "application/json",
                                    "reqEnabled": "Y"}).json()
        self.pub = load_der_public_key(base64.b64decode(j["data"]["publicKey"]))
        self.key = "".join(random.choice(string.ascii_letters + string.digits)
                           for _ in range(16))

    def _enc(self, payload):
        body = json.dumps(payload, ensure_ascii=False,
                          separators=(",", ":")).encode("utf-8")
        e = Cipher(algorithms.AES(self.key.encode()), modes.CBC(self.IV)).encryptor()
        p = sympad.PKCS7(128).padder()
        data = base64.b64encode(
            e.update(p.update(body) + p.finalize()) + e.finalize()).decode()
        cipher = base64.b64encode(self.pub.encrypt(
            json.dumps({"key": self.key, "iv": self.IV.decode()}).encode(),
            asympad.PKCS1v15())).decode()
        return {"data": data, "ciphertext": cipher}

    def _dec(self, s):
        d = Cipher(algorithms.AES(self.key.encode()), modes.CBC(self.IV)).decryptor()
        raw = d.update(base64.b64decode(s)) + d.finalize()
        u = sympad.PKCS7(128).unpadder()
        return json.loads((u.update(raw) + u.finalize()).decode("utf-8"))

    def post(self, name, payload):
        headers = {"Content-Type": "application/json", "secure": "true",
                   "reqEnabled": "Y"}
        for attempt in range(3):
            try:
                r = self.sess.post(self.API + name, json=self._enc(payload),
                                   headers=headers, timeout=40)
                j = r.json()
                if j.get("encrypt") and isinstance(j.get("encryptData"), str):
                    return self._dec(j["encryptData"])
                return j
            except Exception:
                if attempt == 2:
                    raise
                # 会话/密钥过期就重新握手再试
                log("  中香网请求失败，重新握手（第 %d 次）" % (attempt + 1))
                time.sleep(3)
                self.handshake()

    # ---------------- 取数 ----------------
    def full(self):
        """一次拿全量（国内 + 国外），返回 (quotation_date, [item, ...])"""
        d = self.post("getSkuPriceExcelList", {})
        data = d.get("data") or d
        if not isinstance(data, dict) or not ({"china", "foreign"} & set(data)):
            raise RuntimeError("中香网返回结构变了：%s" % str(d)[:300])
        qdate, rows = "", []
        for key, label in MARKET.items():
            blk = data.get(key) or {}
            qdate = qdate or (blk.get("quotationDate") or "")[:10]
            for t in blk.get("skuTypePriceChangeList") or []:
                for p in t.get("skuByTypeAndProducerList") or []:
                    for s in p.get("skuPirceChangeList") or []:
                        rows.append({
                            "market": label,
                            "name": (t.get("skuTypeName") or "").strip(),
                            "producer": (p.get("producer") or "").strip(),
                            "spec": (s.get("spec") or "").strip(),
                            "today": s.get("todayPrice"),
                            "lastday": s.get("lastdayPrice"),
                            "amt": s.get("dailyPriceChangeAmount"),
                            "dod": s.get("dailyPriceChangePercentage"),
                            "mom": s.get("monthPriceChangePercentage"),
                            "yoy": s.get("yearPriceChangePercentage"),
                            "unit": (s.get("skuUnit") or "").strip(),
                        })
        if not rows:
            raise RuntimeError("中香网全量接口返回 0 行")
        return qdate, rows

    def paged(self, page_size=50, max_pages=20):
        """备用：ViewPage 分页（Excel 接口挂了才用）"""
        qdate, rows, seen = "", [], set()
        for pg in range(1, max_pages + 1):
            d = self.post("getSkuPriceViewPage",
                          {"pageNum": pg, "pageSize": page_size, "data": {}})
            qdate = qdate or (d.get("quotationDate") or "")[:10]
            blk = d.get("skuTypePriceChangeList") or {}
            lst = blk.get("list") or []
            if not lst:
                break
            for it in lst:
                k = (it.get("skuTypeName"), it.get("producer"), it.get("spec"))
                if k in seen:
                    continue
                seen.add(k)
                rows.append({
                    "market": "国内" if (it.get("country") in (None, "国内")) else "国外",
                    "name": (it.get("skuTypeName") or "").strip(),
                    "producer": (it.get("producer") or "").strip(),
                    "spec": (it.get("spec") or "").strip(),
                    "today": it.get("todayPrice"),
                    "lastday": it.get("lastdayPrice"),
                    "amt": it.get("dailyPriceChangeAmount"),
                    "dod": it.get("dailyPriceChangePercentage"),
                    "mom": it.get("monthPriceChangePercentage"),
                    "yoy": it.get("yearPriceChangePercentage"),
                    "unit": (it.get("skuUnit") or "").strip(),
                })
            if len(lst) < page_size:
                break
            time.sleep(0.3)
        return qdate, rows


# ---------------- 格式化 ----------------
def fnum(v, nd=2):
    """价格：定两位小数，页面给什么就写什么；空值给空串"""
    if v is None or v == "":
        return ""
    try:
        return "%.*f" % (nd, float(v))
    except (TypeError, ValueError):
        return str(v)


def fpct(v):
    """涨跌幅：-0.0938 → -9.38%；0 → 0.00%"""
    if v is None or v == "":
        return ""
    try:
        return "%.2f%%" % (float(v) * 100)
    except (TypeError, ValueError):
        return str(v)


def key_of(r):
    """一行报价的唯一键（图表/历史序列都用它）"""
    return "%s|%s|%s|%s" % (r["market"], r["name"], r["producer"], r["spec"])


def to_row(r, stamp):
    return [r["market"], r["name"], in_list(r["name"]),
            r["producer"], r["spec"] or "统货",
            fnum(r["today"]), fnum(r["lastday"]), fnum(r["amt"], 2),
            fpct(r["dod"]), fpct(r["mom"]), fpct(r["yoy"]),
            r["unit"], stamp]


def load_hist():
    if os.path.exists(JSON_HIST + ".gz"):
        with gzip.open(JSON_HIST + ".gz", "rt", encoding="utf-8") as f:
            return json.load(f)
    if os.path.exists(JSON_HIST):
        with open(JSON_HIST, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_hist(hist):
    """存成 .gz（一天一个点，一年也就几百 KB）"""
    hist = {k: dict(sorted(v.items())) for k, v in sorted(hist.items()) if v}
    txt = json.dumps(hist, ensure_ascii=False, separators=(",", ":"))
    with open(JSON_HIST, "w", encoding="utf-8") as f:
        f.write(txt)
    with gzip.open(JSON_HIST + ".gz", "wt", encoding="utf-8") as f:
        f.write(txt)


def prev_day(day):
    """yyy-mm-dd 的前一天（只用来给「前日价」找个横轴位置，跨周末会有 1~2 天偏差）"""
    import datetime
    d = datetime.date.fromisoformat(day) - datetime.timedelta(days=1)
    return d.isoformat()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-hist", action="store_true", help="不动历史文件")
    a = ap.parse_args()

    z = ZxQuote()
    z.handshake()
    log("中香网握手成功")

    try:
        qdate, rows = z.full()
        log("全量接口：%d 行（报价日 %s）" % (len(rows), qdate))
    except Exception as e:
        log("⚠️ 全量接口失败（%s），改用分页接口" % e)
        qdate, rows = z.paged()
        log("分页接口：%d 行（报价日 %s）" % (len(rows), qdate))

    if not qdate:
        qdate = time.strftime("%Y-%m-%d")
    stamp = "%s %s" % (qdate, time.strftime("%H:%M"))

    # 去重 + 固定顺序（同品类相邻，便于看）
    uniq = {}
    for r in rows:
        uniq.setdefault(key_of(r), r)
    rows = list(uniq.values())
    # 清单内的排前面（同清单名聚在一起），清单外的沉到后面，方便一眼看对照结果
    rows.sort(key=lambda r: (r["market"], 0 if in_list(r["name"]) else 1,
                             in_list(r["name"]) or r["name"], r["name"],
                             r["producer"], r["spec"]))

    with open(CSV_OUT, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow(to_row(r, stamp))
    log("写入 %s（%d 行）" % (os.path.basename(CSV_OUT), len(rows)))

    # 清单对照统计（对标 medicine_list，口径同康美线）
    in_rows = sum(1 for r in rows if in_list(r["name"]))
    in_cats = sorted(set(in_list(r["name"]) for r in rows if in_list(r["name"])))
    out_cats = sorted(set(r["name"] for r in rows if not in_list(r["name"])))
    log("对标 medicine_list：清单内 %d 行 / %d 味；清单外品类 %d 个（%s）"
        % (in_rows, len(in_cats), len(out_cats), "、".join(out_cats)))

    # 涨跌面统计，日志里给一眼能看懂的结论
    def pv(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    up = sum(1 for r in rows if (pv(r["dod"]) or 0) > 0)
    down = sum(1 for r in rows if (pv(r["dod"]) or 0) < 0)
    log("较前日：涨 %d / 跌 %d / 平 %d" % (up, down, len(rows) - up - down))

    if a.no_hist:
        log("--no-hist，跳过历史累积")
        return

    hist = load_hist()
    added = 0
    for r in rows:
        k = key_of(r)
        ser = hist.setdefault(k, {})
        p = pv(r["today"])
        if p is None:
            continue
        if qdate not in ser:
            added += 1
        ser[qdate] = round(p, 4)
        # 官方给的「前日价」也记成前一天的点：第一天图就有两个点，之后自然被真实数据覆盖
        lp = pv(r["lastday"])
        if lp is not None:
            d1 = prev_day(qdate)
            ser.setdefault(d1, round(lp, 4))
    save_hist(hist)
    log("历史累积：新增 %d 行今日价，序列共 %d 条（%s）"
        % (added, len(hist), os.path.basename(JSON_HIST) + ".gz"))


if __name__ == "__main__":
    main()
