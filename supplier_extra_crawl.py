# -*- coding: utf-8 -*-
r"""
康美中药城 / 中香网「供货」信息抓取 —— 供应商数据线（第 2、3 站）

产出两个 CSV，表头与药通网 supplier_data.csv **完全一致**（首列「更新时间」），
这样 supplier_multi_push.py 能用同一套逻辑推三个工作表：
    supplier_km.csv   康美中药城 供货（约 4980 条）
    supplier_zx.csv   中香网 供货（约 427 条）

================================ 康美 ================================
列表：GET https://www.kmb2b.com/supplyInfo/supplyInfo_supplyInfoListHtml.action
        ?page=N&viewSupplyInfo.byTimeDesc=true      （上架时间倒序，20 条/页，约 249 页）
    · 卡片字段（div.goods-item）：
        title-selling-point → 品名
        spec-config         → "规格/产地"（最后一段是产地）
        place-info          → 货源所在地
        price-box           → 数量（这个站供货列表没有价格）
        tel-box             → 电话（**不登录显示「登录后可见」**）
        i.res-tag           → 认证标记
    · 这个站没有 发布时间 / 剩余天数 / 单价 / 供货说明 → 对应列留空，不编。
    · 详情页有 联系人 / 会员类型，但一页 20 条要 20 次请求，全量 4980 条约 5000 次，
      耗时 15 分钟以上，性价比太低 → 不抓详情，联系人为空（手机号已经足够联系）。
    · 电话必须登录：GET /ajaxJson/unionLogin.action?loginName=..&passwordcode=md5(密码)
      &veCode=&registerPlatform=&callback=jsonp1   （jsonp 只能走 GET）

================================ 中香网 ================================
列表：POST /api/front/skuSupplyController/querySupplyListPage   （AES-128-CBC + RSA）
      参数 {"pageNum":N,"pageSize":20,"data":{}}，请求头 secure:true、reqEnabled:Y
    · total 427 条，20 条/页 → 22 页
    · 关键字段：supplyTitle 品名 / spec 规格 / originCountry 产地 / sourceProvinceNames 货源地
      priceType(2=面议) priceMin priceMax / stockType(1=大量) stockNum stockUnit(1=公斤)
      supplyLabels 标签 / createdTime / supplyExpiryTime
    · 平台不公开联系人与电话（linkmanName/linkmanPhone 恒为 null），两列留空。
    · 加密细节与求购线一致，见 purchase_crawl.py 的 ZxPurchase。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe supplier_extra_crawl.py                 # 康美 + 中香 全量
    .venv\Scripts\python.exe supplier_extra_crawl.py --only zx       # 只抓中香
    .venv\Scripts\python.exe supplier_extra_crawl.py --max-pages 3   # 每站只翻 3 页（测试）
    KM_USER=.. KM_PASS=.. 供康美登录用（不设则电话列会是「登录后可见」）
"""
import argparse
import base64
import csv
import datetime
import hashlib
import json
import os
import random
import re
import string
import time
import traceback

import requests

try:                      # 中香网接口要加密；康美不需要
    from cryptography.hazmat.primitives import padding as sympad
    from cryptography.hazmat.primitives.asymmetric import padding as asympad
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    HAS_CRYPTO = True
except Exception:
    HAS_CRYPTO = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_KM = os.path.join(BASE_DIR, "supplier_km.csv")
CSV_ZX = os.path.join(BASE_DIR, "supplier_zx.csv")

# 与药通网 supplier_data.csv 一致（首列「更新时间」，推送时挪到末列「更新日期」）
HEADER = ["更新时间", "品名", "规格", "产地", "货源所在地", "单价",
          "数量", "联系人", "手机", "供货说明", "发布日期", "剩余天数", "实名认证"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")
TAG_RE = re.compile(r"<[^>]+>")
GAP = 0.25          # 请求间隔，两个站都不算敏感，0.25 秒足够稳


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean(s):
    """去标签 / 转义 / 多余空白。"""
    if s is None:
        return ""
    s = TAG_RE.sub("", str(s))
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return " ".join(s.split()).strip()


def new_session(referer=""):
    s = requests.Session()
    # ⚠️ 绝不能读系统代理：用户开 VPN 时 system proxy 会让国内站超时
    s.trust_env = False
    h = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
    if referer:
        h["Referer"] = referer
    s.headers.update(h)
    return s


def get_retry(sess, url, tries=3, **kw):
    """5xx / 连接异常重试 3 次。"""
    last = None
    for i in range(tries):
        try:
            r = sess.get(url, timeout=kw.pop("timeout", 40), **kw)
            if r.status_code >= 500:
                last = RuntimeError(f"HTTP {r.status_code}")
            else:
                return r
        except requests.RequestException as e:
            last = e
        time.sleep(1.5 * (i + 1))
    raise last


def md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEADER)
        for r in rows:
            w.writerow([r.get(k, "") for k in HEADER])
    log(f"  → {os.path.basename(path)}  {len(rows)} 行")


# ================================================================ 康美供货
class KmSupply:
    HOST = "https://www.kmb2b.com"
    LIST = HOST + "/supplyInfo/supplyInfo_supplyInfoListHtml.action"
    ENTRY = HOST + "/supplyInfo.html"
    LOGIN_PAGE = HOST + "/login/index.html"
    LOGIN = HOST + "/ajaxJson/unionLogin.action"
    PAGE_SIZE = 20

    def __init__(self):
        self.sess = new_session(self.LIST + "?viewSupplyInfo.byTimeDesc=true")
        self.logged_in = False

    def login(self, user, pwd):
        """jsonp 接口只能 GET（页面里是 $.ajax({dataType:"jsonp"})）。"""
        try:
            get_retry(self.sess, self.LOGIN_PAGE, timeout=25)
            r = get_retry(self.sess, self.LOGIN, timeout=30, params={
                "loginName": user, "passwordcode": md5_hex(pwd),
                "veCode": "", "registerPlatform": "", "callback": "jsonp1"})
            body = r.text
            self.logged_in = ('"returnObject":"1.' in body) or ("returnObject:'1." in body)
            log(f"  康美登录 {'成功' if self.logged_in else '失败'}：{clean(body)[:120]}")
        except Exception as e:
            log(f"  康美登录异常：{type(e).__name__}: {str(e)[:120]}")

    @staticmethod
    def parse_card(block):
        def one(pat, s=block):
            m = re.search(pat, s, re.S)
            return m.group(1) if m else ""
        url = one(r'href="(https://www\.kmb2b\.com/statichtml/supply/info-\d+\.html)"')
        name = clean(one(r'class="title-selling-point">(.*?)<'))
        spec_raw = clean(one(r'class="spec-config">(.*?)</div>'))
        # "0.5-0.8cm，圆片/甘肃" → 规格 / 产地
        if "/" in spec_raw:
            spec, origin = spec_raw.rsplit("/", 1)
        else:
            spec, origin = spec_raw, ""
        qty = clean(one(r'class="price-box">(.*?)</div>'))
        tel = clean(one(r'class="tel-box">(.*?)</div>')).replace("电话：", "").strip()
        return {
            "品名": name,
            "规格": spec.strip(),
            "产地": origin.strip(),
            "货源所在地": clean(one(r'class="place-info">(.*?)</div>')),
            "单价": "",                      # 这个站供货列表没有价格
            "数量": qty,
            "联系人": "",                    # 只在详情页，抓全量要多 5000 次请求，放弃
            "手机": "" if tel == "登录后可见" else tel,
            "供货说明": "",
            "发布日期": "",                  # 页面没有发布时间
            "剩余天数": "",
            "实名认证": "已认证" if 'class="res-tag' in block else "",
            "_id": re.search(r"info-(\d+)", url).group(1) if url else name,
        }

    def fetch(self, max_pages=None):
        rows, seen = [], set()
        pg, empty = 1, 0
        while True:
            if max_pages and pg > max_pages:
                break
            time.sleep(GAP)
            r = get_retry(self.sess, self.LIST, timeout=40,
                          params={"page": pg, "viewSupplyInfo.byTimeDesc": "true"})
            r.encoding = "utf-8"
            html = r.text
            parts = html.split('<div class="goods-item">')[1:]
            if not parts:
                if empty == 1:
                    break
                empty += 1
                pg += 1
                continue
            empty = 0
            new = 0
            for p in parts:
                d = self.parse_card(p[:2600])
                if not d["品名"]:
                    continue
                if d["_id"] in seen:
                    continue
                seen.add(d["_id"])
                rows.append(d)
                new += 1
                if pg % 25 == 0:
                    log(f"    {pg} 页 / {len(rows)} 条：{d['品名']}")
            if new == 0:
                break
            pg += 1
        log(f"  康美供货 {len(rows)} 条（{pg - 1} 页，"
            f"{'登录态，电话可见' if self.logged_in else '未登录，电话会被打成「登录后可见」'}）")
        return rows


# ================================================================ 中香网供货
class ZxSupply:
    HOST = "https://www.zhongxiangwang.cn"
    PAGE = HOST + "/supply/index"
    PUBKEY = HOST + "/api/front/encryptController/getEncryptPubkey"
    LIST = HOST + "/api/front/skuSupplyController/querySupplyListPage"
    IV = b"0392039203920300"
    PAGE_SIZE = 20
    STOCK_UNIT = {0: "", 1: "公斤", 2: "吨", 3: "克"}

    def __init__(self):
        self.sess = new_session(self.PAGE)
        self.sess.headers.update({"Terminal": "PC", "Version": "1.0.0",
                                  "Origin": self.HOST})
        self.key = None
        self.pub = None

    def handshake(self):
        if not HAS_CRYPTO:
            raise RuntimeError("中香网接口要加密，请先 pip install cryptography")
        get_retry(self.sess, self.PAGE, timeout=25)
        j = self.sess.post(self.PUBKEY, json={}, timeout=30,
                           headers={"Content-Type": "application/json",
                                    "reqEnabled": "Y"}).json()
        self.pub = load_der_public_key(base64.b64decode(j["data"]["publicKey"]))
        self.key = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(16))

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

    def fetch(self, max_pages=None):
        for attempt in range(3):
            try:
                self.handshake()
                break
            except Exception as e:
                if attempt == 2:
                    raise
                log(f"  中香网握手第 {attempt + 1} 次失败（{type(e).__name__}），重试")
                time.sleep(3 + attempt * 3)
        rows, seen = [], set()
        total = None
        for pg in range(1, (max_pages or 200) + 1):
            time.sleep(GAP)
            j = self.sess.post(self.LIST, json=self._enc(
                {"pageNum": pg, "pageSize": self.PAGE_SIZE, "data": {}}),
                timeout=30, headers={"Content-Type": "application/json",
                                     "secure": "true", "reqEnabled": "Y"}).json()
            if j.get("code") != 200:
                log(f"  ⚠️ 中香网第 {pg} 页返回 {j.get('code')} {j.get('message')}")
                break
            d = j.get("data")
            if j.get("encrypt") and isinstance(d, str):
                d = self._dec(d)
            total = (d or {}).get("total") or total
            lst = (d or {}).get("list") or []
            if not lst:
                break
            for it in lst:
                sid = str(it.get("supplyId") or "")
                if sid and sid in seen:
                    continue
                seen.add(sid)
                rows.append(self.to_row(it))
            if len(lst) < self.PAGE_SIZE:
                break
        log(f"  中香网供货 {len(rows)} 条（平台 total={total}）")
        return rows

    @classmethod
    def to_row(cls, it):
        # 价格：priceType=2 是面议，其余按 min/max 出区间
        if it.get("priceType") == 2 or it.get("priceMin") in (None, ""):
            price = "面议"
        else:
            pmin, pmax = it.get("priceMin"), it.get("priceMax")
            base = f"{pmin:g}" if isinstance(pmin, (int, float)) else str(pmin)
            if isinstance(pmax, (int, float)) and pmax and pmax != pmin:
                price = f"{base}~{pmax:g} 元/公斤"
            else:
                price = f"{base} 元/公斤"
        # 库存：stockType=1 表示「大量」；stockUnit=1 是公斤（10000 → 10 吨）
        if it.get("stockType") == 1:
            qty = "大量"
        elif it.get("stockNum") is None:
            qty = ""
        else:
            num = it["stockNum"]
            unit = cls.STOCK_UNIT.get(it.get("stockUnit"), "")
            if unit == "公斤" and num >= 1000:
                qty = f"{num / 1000:g} 吨"
            elif unit:
                qty = f"{num:g} {unit}"
            else:
                qty = f"{num:g}"
        # 剩余天数 = 截止日期 - 今天。
        # 中香网用 supplyValidityDay = -1 表示「长期有效」，后台把截止日期写成 2099-01-01，
        # 422/427 条都是这种，直接算会得出 26000 多天，所以单独标成「长期」。
        days = ""
        exp = (it.get("supplyExpiryTime") or "")[:10]
        if it.get("supplyValidityDay") == -1 or exp.startswith("2099"):
            days = "长期"
        elif exp:
            try:
                days = str((datetime.date.fromisoformat(exp) - datetime.date.today()).days)
            except ValueError:
                days = ""
        origin = clean(it.get("originCountry"))
        return {
            "品名": clean(it.get("supplyTitle")),
            "规格": clean(it.get("spec")),
            "产地": "" if origin in ("不限", "-") else origin,
            "货源所在地": clean(it.get("sourceProvinceNames")),
            "单价": price,
            "数量": qty,
            "联系人": "",                       # 平台不公开
            "手机": "",                         # 平台不公开
            "供货说明": clean(it.get("supplyLabels")),
            "发布日期": (it.get("createdTime") or "")[:10],
            "剩余天数": days,
            "实名认证": "",
            "_id": str(it.get("supplyId") or ""),
        }


# ================================================================ 主流程
def sort_rows(rows):
    """按品名分组，组内发布日期新的在前（跟药通网表的观感一致）。两次稳定排序实现。"""
    rows.sort(key=lambda r: r.get("发布日期") or "", reverse=True)
    rows.sort(key=lambda r: r.get("品名") or "")
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["km", "zx"], help="只抓一个站")
    ap.add_argument("--max-pages", type=int, default=None, help="每站最多翻 N 页（测试用）")
    args = ap.parse_args()

    today = time.strftime("%Y-%m-%d")
    failed = []

    if args.only in (None, "km"):
        log("A. 康美中药城「供货」")
        try:
            km = KmSupply()
            user, pwd = os.environ.get("KM_USER", "").strip(), os.environ.get("KM_PASS", "").strip()
            if user and pwd:
                km.login(user, pwd)
            else:
                log("  未设 KM_USER / KM_PASS，跳过登录（电话列会是「登录后可见」，会被清空）")
            rows = km.fetch(max_pages=args.max_pages)
            for r in rows:
                r["更新时间"] = today
            write_csv(CSV_KM, sort_rows(rows))
        except Exception:
            log("  ❌ 康美供货抓取失败（中香不受影响）")
            log(traceback.format_exc().strip())
            failed.append("km")

    if args.only in (None, "zx"):
        log("B. 中香网「供货」")
        try:
            rows = ZxSupply().fetch(max_pages=args.max_pages)
            for r in rows:
                r["更新时间"] = today
            write_csv(CSV_ZX, sort_rows(rows))
        except Exception:
            log("  ❌ 中香网供货抓取失败")
            log(traceback.format_exc().strip())
            failed.append("zx")

    if failed:
        raise SystemExit(f"有站点失败：{','.join(failed)}")
    log("全部完成")


if __name__ == "__main__":
    main()
