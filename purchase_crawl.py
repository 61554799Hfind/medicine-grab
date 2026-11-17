# -*- coding: utf-8 -*-
r"""
三个网站的「个人求购 / 需求」信息抓取 —— 采购商数据线

来源（实测 2026-11-17）
------------------------------------------------------------------
A. 药通网「个人求购」      https://www.yt1998.com/demandInfo.html
     GET /gongqiu/queryGQ!getList.do  gqflg=1  空品名翻页
     全站约 233 条；免登录，手机号直接给。
     字段：ycnam 品名 / guige 规格 / chandi 产地 / huoyuandi 货源所在地 /
           pri 单价 / shul 数量 / unam 联系人 / mob 手机 / notemop 备用电话 /
           remarks 求购说明 / addr 地址 / dtm 发布日期 / shengyuts 剩余天数 /
           personrenzheng 实名认证 / gqid 编号

B. 康美中药城「需求信息」  https://www.kmb2b.com/statichtml/purchase/index.html
    服务端渲染 HTML，一页 19 条（站点自报「共1页」，就是全部）。
     列表：品名 / 剩余X天 / 规格 / 产地 / 数量 / 电话
     详情：有效期 / 仓储所在地 / 质量要求 / 联系人 / 认证信息 / 会员类型 /
           经营所在地 / 包装要求 / 资质要求
     ⚠️ 电话要登录才给（页面写「登录后可见」）；本人不登录时该列留空。
     ⚠️ 康美**没有发布时间**，只有有效期与剩余天数 → 排序按剩余天数从大到小，
        等价于「发布最早的在前面」。

C. 中香网「求购」          https://www.zhongxiangwang.cn/supply/index
     纯前端 SPA，接口要 AES-128-CBC + RSA 加密（密钥从接口现取）：
         POST /api/front/encryptController/getEncryptPubkey   -> RSA 公钥
         POST /api/front/skuPurchaseNewController/queryPurchaseListPage
         body = {data: base64(AES(json)), ciphertext: base64(RSA({key,iv}))}
         header = secure: true, reqEnabled: Y, Terminal: PC, Version: 1.0.0
     全站约 27 条。字段：skuName 品名 / purchaseSpec 规格 / producer 产地 /
         producerNum+producerNumUnit 求购量 / purchasePriceMin/Max 价格 /
         purchaseLabel 合作类型 / createdTime 发布日期 / purchaseExpiryTime 截止 /
         linkmanName 联系人 / linkmanPhone 电话（平台未公开时为空）
     响应体若 encrypt=true，用同一个 AES key 解回来。

通用
------------------------------------------------------------------
⚠️ 三个站都是国内站，本机开着 VPN 时必须 trust_env=False 直连。
⚠️ 康美登录：可选。设环境变量 KM_USER / KM_PASS（手机号 + 密码）就会先登录再抓，
   登录后电话才有值；没设就照常抓，电话列留空。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe purchase_crawl.py            # 抓三个站
    .venv\Scripts\python.exe purchase_crawl.py --only km  # 只抓康美
    .venv\Scripts\python.exe purchase_crawl.py --dump     # 抓完顺带打印每条字段
"""
import argparse
import base64
import csv
import hashlib
import json
import os
import random
import re
import string
import time
import traceback

import requests
from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives.asymmetric import padding as asympad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.serialization import load_der_public_key

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_JSON = os.path.join(BASE_DIR, "purchase_data.json")
CSV_YTW = os.path.join(BASE_DIR, "purchase_ytw.csv")
CSV_KM = os.path.join(BASE_DIR, "purchase_km.csv")
CSV_ZX = os.path.join(BASE_DIR, "purchase_zx.csv")
# 某个站整体失败时写这个标记文件；CI 的「结果自检」看到它就把 job 判红，
# 但不会拦住后面的推送步骤 —— 三个站互相独立，一个站挂了不该拖累另外两个。
FAIL_FILE = os.path.join(BASE_DIR, "purchase_failed.txt")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

# ---------------- 表头（顺序即落表顺序，最后一列是更新日期） ----------------
HEADER_YTW = ["品名", "规格", "产地", "货源所在地", "单价", "数量",
              "联系人", "手机", "备用电话", "求购说明", "地址",
              "发布日期", "剩余天数", "实名认证", "更新日期"]
HEADER_KM = ["品名", "规格", "产地", "数量", "联系电话", "联系人",
             "剩余天数", "有效期", "仓储所在地", "质量要求", "包装要求",
             "资质要求", "认证信息", "会员类型", "经营所在地", "详情链接", "更新日期"]
HEADER_ZX = ["品名", "规格", "产地", "求购量", "单价", "合作类型",
             "联系人", "电话", "发布日期", "截止日期", "更新日期"]

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[\s\u00a0\u3000]+")


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean(s):
    if s is None:
        return ""
    s = TAG_RE.sub("", str(s))
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return SPACE_RE.sub(" ", s).strip()


def new_session(referer=""):
    s = requests.Session()
    # 国内站：绝不能读系统代理（用户开 VPN 时会超时）
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"})
    if referer:
        s.headers["Referer"] = referer
    return s


def decode_html(resp):
    """国内站有的是 gbk，按 meta 声明解，兜底 utf-8 / gb18030。"""
    m = re.search(rb'charset=["\']?([\w-]+)', resp.content[:3000], re.I)
    enc = m.group(1).decode("ascii", "ignore").lower() if m else "utf-8"
    for cand in (enc, "utf-8", "gb18030"):
        try:
            return resp.content.decode(cand)
        except (LookupError, UnicodeDecodeError):
            continue
    return resp.text


def md5_hex(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def get_retry(sess, url, attempts=3, timeout=30, **kw):
    """带重试的 GET：海外 runner 访问国内小站偶尔会 5xx / 握手超时，重试两轮基本就过。"""
    last = None
    for i in range(attempts):
        try:
            r = sess.get(url, timeout=timeout, **kw)
            if r.status_code < 500:
                return r
            last = RuntimeError(f"HTTP {r.status_code}")
        except Exception as e:
            last = e
        if i < attempts - 1:
            time.sleep(2 + i * 3)
    raise last


# ================================================================ A. 药通网
class YtwDemand:
    HOST = "https://www.yt1998.com"
    ENTRY = HOST + "/demandInfo.html"
    LIST = HOST + "/gongqiu/queryGQ!getList.do"
    PAGE_SIZE = 200
    MAX_PAGES = 60

    def __init__(self):
        self.sess = new_session(self.ENTRY)
        self.sess.headers.update({"X-Requested-With": "XMLHttpRequest",
                                  "Accept": "application/json, text/javascript, */*; q=0.01"})

    def warmup(self):
        self.sess.cookies.set(
            "zshcookiename",
            "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(8)),
            domain="www.yt1998.com")
        r = self.sess.get(self.ENTRY, timeout=25)
        log(f"  药通网入口 {r.status_code}，cookie={list(self.sess.cookies.keys())}")

    def fetch(self):
        self.warmup()
        rows, seen = [], set()
        for pi in range(self.MAX_PAGES):
            for attempt in range(4):
                try:
                    time.sleep(0.2)
                    j = self.sess.get(self.LIST, timeout=30, params={
                        "totalCount": 9999, "gqflg": "1", "ycnam": "", "chandi": "",
                        "guige": "", "order_by": "", "pageIndex": pi,
                        "pageSize": self.PAGE_SIZE,
                    }).json()
                    break
                except Exception as e:
                    if attempt == 3:
                        log(f"  ⚠️ 药通网第 {pi} 页失败：{type(e).__name__}: {e}")
                        j = {}
                    else:
                        time.sleep(2 + attempt * 2)
            data = j.get("data") or []
            if not data:
                break
            for d in data:
                gid = str(d.get("gqid") or "").strip()
                key = gid or f"{d.get('mob')}|{d.get('dtm')}|{d.get('addr')}"
                if key in seen:
                    continue
                seen.add(key)
                rows.append(d)
            if len(data) < self.PAGE_SIZE:
                break
        log(f"  药通网求购 {len(rows)} 条")
        return [self.to_row(d) for d in rows]

    @staticmethod
    def to_row(d):
        raw_rzh = str(d.get("personrenzheng") or "")
        return {
            "品名": clean(d.get("ycnam")),
            "规格": clean(d.get("guige")),
            "产地": clean(d.get("chandi")),
            "货源所在地": clean(d.get("huoyuandi")),
            "单价": clean(d.get("pri")),
            "数量": clean(d.get("shul")),
            "联系人": clean(d.get("unam")),
            "手机": clean(d.get("mob")),
            "备用电话": clean(d.get("notemop")),
            "求购说明": clean(d.get("remarks")),
            "地址": clean(d.get("addr")),
            "发布日期": clean(d.get("dtm")),
            "剩余天数": clean(d.get("shengyuts")),
            "实名认证": "已认证" if ("renzheng.gif" in raw_rzh or "实名认证" in raw_rzh) else "",
            "_id": clean(d.get("gqid")),
        }


# ================================================================ B. 康美中药城
class KmDemand:
    HOST = "https://www.kmb2b.com"
    LIST = HOST + "/statichtml/purchase/index.html"
    LOGIN = HOST + "/html/loginPage.htm"

    def __init__(self):
        self.sess = new_session(self.LIST)
        self.logged_in = False

    def login(self, user, pwd):
        """可选登录：登录后详情页的「联系电话」才不是「登录后可见」。

        页面 loginPage.js 里用的是 $.ajax({dataType:"jsonp"})，jQuery 对 jsonp
        一律发 GET（POST 会拿到通用失败码 0），所以这里也必须用 GET。
        参数：loginName=encodeURI(手机号) / passwordcode=md5(密码) /
              veCode="" / registerPlatform=""；callback 由 jQuery 自动加，随便给一个。
        返回：returnObject 1.1 / 1.2 = 成功；-1 = 账号被禁用；-2 = 登录过于频繁；
              其他（0）= 密码不对，message 是已错次数，连错 5 次锁 2 小时。
        """
        try:
            self.sess.get(self.LOGIN, timeout=25)
            r = self.sess.get(self.HOST + "/ajaxJson/unionLogin.action",
                              params={"loginName": requests.utils.quote(user),
                                      "passwordcode": md5_hex(pwd),
                                      "veCode": "", "registerPlatform": "",
                                      "callback": "jsonp1"},
                              timeout=30)
            body = r.text
            m = re.search(r'returnObject["\']?\s*:\s*["\']?(-?[\d.]+)', body)
            code = m.group(1) if m else ""
            mc = re.search(r'message["\']?\s*:\s*["\']?([^"\',}]*)', body)
            self.logged_in = code in ("1.1", "1.2")
            if self.logged_in:
                log("  康美登录成功")
            else:
                hint = {"-1": "账号被系统禁用", "-2": "登录过于频繁，稍后再试"}.get(code, "密码不对")
                log(f"  康美登录失败（{hint}，returnObject={code or '?'}"
                    f"{'，已错 ' + mc.group(1) + ' 次' if mc else ''}）：{clean(body)[:120]}")
        except Exception as e:
            log(f"  康美登录异常：{type(e).__name__}: {e}")

    def fetch(self, with_detail=True):
        r = get_retry(self.sess, self.LIST)
        html = decode_html(r)
        if "登录后可见" not in html and "goods-item" not in html:
            log(f"  ⚠️ 康美列表异常（{r.status_code}，{len(html)} 字节）")
        cards = re.findall(r'<div class="goods-item">(.*?)</div>\s*</a>', html, re.S)
        if not cards:
            cards = re.findall(r'<div class="goods-item">(.*?)(?=<div class="goods-item">|\Z)', html, re.S)
        rows = []
        for c in cards:
            m = re.search(r'href="([^"]*purchase/info-[^"]+)"', c)
            url = m.group(1) if m else ""
            title_m = re.search(r'<div class="title-selling-point">(.*?)</div>', c, re.S)
            title = clean(title_m.group(1)) if title_m else ""
            days_m = re.search(r"剩余\s*(\d+)\s*天", title)
            name = SPACE_RE.sub(" ", re.sub(r"剩余\s*\d+\s*天", "", title)).strip()
            fields = {}
            for lab, val in re.findall(
                    r'<p class="p-item"><label>([^<]+)</label>\s*(.*?)</p>', c, re.S):
                fields[lab.strip("：: ")] = clean(val)
            raw = c
            row = {
                "品名": name,
                "规格": fields.get("规格", ""),
                "产地": fields.get("产地", ""),
                "数量": fields.get("数量", ""),
                "联系电话": fields.get("电话", ""),
                "联系人": "",
                "剩余天数": days_m.group(1) if days_m else "",
                "有效期": "",
                "仓储所在地": "",
                "质量要求": "",
                "包装要求": "",
                "资质要求": "",
                "认证信息": "个人认证" if "res-tag person" in raw else "",
                "会员类型": "",
                "经营所在地": "",
                "详情链接": url,
                "_id": (re.search(r"info-(\d+)", url).group(1) if url else name),
            }
            if with_detail and url:
                try:
                    time.sleep(0.2)
                    d = decode_html(self.sess.get(url, timeout=30))
                    det = {}
                    for lab, val in re.findall(
                            r'<p class="m-item"><label>([^<]+)</label>\s*(.*?)</p>', d, re.S):
                        det[lab.strip("：: ").replace(" ", "")] = clean(val)
                    row.update({
                        "有效期": det.get("有效期", ""),
                        "仓储所在地": det.get("仓储所在地", ""),
                        "质量要求": det.get("质量要求", ""),
                        "包装要求": det.get("包装要求", ""),
                        "资质要求": det.get("资质要求", ""),
                        "联系人": det.get("联系人", ""),
                        "认证信息": det.get("认证信息", row["认证信息"]),
                        "会员类型": det.get("会员类型", ""),
                        "经营所在地": det.get("经营所在地", ""),
                    })
                    if det.get("联系电话") and det["联系电话"] != "登录后可见":
                        row["联系电话"] = det["联系电话"]
                except Exception as e:
                    log(f"  ⚠️ 康美详情 {url} 失败：{type(e).__name__}")
            rows.append(row)
        log(f"  康美需求 {len(rows)} 条（{'已登录，电话可见' if self.logged_in else '未登录，电话留空'}）")
        return rows


# ================================================================ C. 中香网
class ZxPurchase:
    HOST = "https://www.zhongxiangwang.cn"
    PAGE = HOST + "/supply/index"
    PUBKEY = HOST + "/api/front/encryptController/getEncryptPubkey"
    LIST = HOST + "/api/front/skuPurchaseNewController/queryPurchaseListPage"
    IV = b"0392039203920300"
    PAGE_SIZE = 20
    MAX_PAGES = 40
    # 求购量单位（实测只有 0/1）：1 = 吨（接口给的是千克数，要 /1000）；0 = 发布方没填单位
    UNIT = {0: "", 1: "吨", 2: "千克", 3: "克"}

    def __init__(self):
        self.sess = new_session(self.PAGE)
        self.sess.headers.update({"Terminal": "PC", "Version": "1.0.0",
                                  "Origin": self.HOST})
        self.key = None
        self.pub = None

    def handshake(self):
        self.sess.get(self.PAGE, timeout=25)
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

    def fetch(self):
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
        for pg in range(1, self.MAX_PAGES + 1):
            time.sleep(0.2)
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
            lst = (d or {}).get("list") or []
            if not lst:
                break
            for it in lst:
                pid = str(it.get("purchaseId") or "")
                if pid and pid in seen:
                    continue
                seen.add(pid)
                rows.append(self.to_row(it))
            if len(lst) < self.PAGE_SIZE:
                break
        log(f"  中香网求购 {len(rows)} 条")
        return rows

    @classmethod
    def to_row(cls, it):
        num, unit = it.get("producerNum"), it.get("producerNumUnit")
        qty = ""
        if num not in (None, ""):
            try:
                v = float(num)
                try:
                    u = cls.UNIT.get(int(unit), "")
                except (TypeError, ValueError):
                    u = ""
                if u == "吨":
                    qty = f"{v / 1000:.2f} 吨"
                elif u:
                    qty = f"{v:g} {u}"
                else:
                    qty = f"{v:g}"
            except (TypeError, ValueError):
                qty = f"{num}"
        # priceType 2 = 面议；有 min/max 就给区间
        pmin, pmax = it.get("purchasePriceMin"), it.get("purchasePriceMax")
        if pmin not in (None, "") and pmax not in (None, ""):
            price = f"{pmin}~{pmax}" if str(pmin) != str(pmax) else str(pmin)
        elif pmin not in (None, ""):
            price = str(pmin)
        else:
            price = "面议" if it.get("priceType") == 2 else ""
        return {
            "品名": clean(it.get("skuName")),
            "规格": clean(it.get("purchaseSpec")),
            "产地": clean(it.get("producer")),
            "求购量": qty,
            "单价": price,
            "合作类型": clean(it.get("purchaseLabel")),
            "联系人": clean(it.get("linkmanName")),
            "电话": clean(it.get("linkmanPhone")),
            "发布日期": (it.get("createdTime") or "")[:10],
            "截止日期": (it.get("purchaseExpiryTime") or "")[:10],
            # 中香网的求购卡片没有详情页可跳（前端没有 /supply/detail 路由），
            # 拼个假链接反而误导，所以不给「详情链接」列。
            "_id": str(it.get("purchaseId") or ""),
        }


# ================================================================ 落盘
def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow([r.get(k, "") for k in header])
    log(f"  → {os.path.basename(path)}  {len(rows)} 行")


def is_fresh(date_str, max_age_days):
    """发布日期在 max_age_days 天内；日期解析不了就保留（宁可留着也别误删）。"""
    try:
        dt = time.strptime((date_str or "").strip()[:10], "%Y-%m-%d")
    except ValueError:
        return True
    return (time.time() - time.mktime(dt)) / 86400 <= max_age_days


def sort_key(row, field, numeric=False):
    v = (row.get(field) or "").strip()
    if numeric:
        try:
            return (0, float(v))
        except ValueError:
            return (1, 0.0)
    return (0, v) if v else (1, "")


def probe_hosts():
    """开跑前先敲一下三个站，把状态码/耗时打出来 —— CI 上出问题一眼能看出是哪个站被墙/被拦。"""
    for name, url in (("药通网", YtwDemand.ENTRY),
                      ("康美", KmDemand.LIST),
                      ("中香网", ZxPurchase.PAGE)):
        t0 = time.time()
        try:
            r = new_session(url).get(url, timeout=20)
            log(f"  连通 {name}: HTTP {r.status_code}，{len(r.content)} 字节，{time.time() - t0:.1f}s")
        except Exception as e:
            log(f"  连通 {name}: ❌ {type(e).__name__}: {str(e)[:100]}")


FAILED = []


def report_fail(key, name):
    tb = traceback.format_exc().strip()
    log(f"  ❌ {name} 整体失败，跳过该站（另外两个站照常）")
    log(tb)
    FAILED.append(f"{name} | {tb.splitlines()[-1][:200]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["ytw", "km", "zx"], help="只抓一个站")
    ap.add_argument("--no-detail", action="store_true", help="康美不抓详情页（省请求，字段会空）")
    ap.add_argument("--dump", action="store_true", help="打印每条记录的关键字段")
    ap.add_argument("--max-age-days", type=int, default=730,
                    help="药通网只保留最近 N 天发布的（默认 730=2 年，0=不过滤）")
    args = ap.parse_args()

    today = time.strftime("%Y-%m-%d")
    data = {}

    probe_hosts()

    if args.only in (None, "ytw"):
        log("A. 药通网「个人求购」")
        try:
            rows = YtwDemand().fetch()
            if args.max_age_days:
                kept = [r for r in rows if is_fresh(r["发布日期"], args.max_age_days)]
                if len(kept) != len(rows):
                    log(f"  过滤掉 {len(rows) - len(kept)} 条 {args.max_age_days} 天前的老帖"
                        f"（平台仍在刷新剩余天数，但日期太老）")
                rows = kept
            rows.sort(key=lambda r: (sort_key(r, "发布日期"), sort_key(r, "品名")))
            for r in rows:
                r["更新日期"] = today
            data["ytw"] = rows
            write_csv(CSV_YTW, HEADER_YTW, rows)
        except Exception:
            report_fail("ytw", "药通网")
            data["ytw"] = []   # 不写空 CSV：让推送步骤跳过该 tab，保留昨天那份，别把表刷白

    if args.only in (None, "km"):
        log("B. 康美中药城「需求信息」")
        try:
            km = KmDemand()
            user, pwd = os.environ.get("KM_USER", "").strip(), os.environ.get("KM_PASS", "").strip()
            if user and pwd:
                km.login(user, pwd)
            else:
                log("  未设 KM_USER / KM_PASS，跳过登录（电话列会留空）")
            rows = km.fetch(with_detail=not args.no_detail)
            # 康美没有发布时间 → 剩余天数越大 = 发布越早
            rows.sort(key=lambda r: (-(int(r["剩余天数"]) if r["剩余天数"].isdigit() else -1),
                                     r["品名"]))
            for r in rows:
                r["更新日期"] = today
            data["km"] = rows
            write_csv(CSV_KM, HEADER_KM, rows)
        except Exception:
            report_fail("km", "康美中药城")
            data["km"] = []

    if args.only in (None, "zx"):
        log("C. 中香网「求购」")
        try:
            rows = ZxPurchase().fetch()
            rows.sort(key=lambda r: (sort_key(r, "发布日期"), sort_key(r, "品名")))
            for r in rows:
                r["更新日期"] = today
            data["zx"] = rows
            write_csv(CSV_ZX, HEADER_ZX, rows)
        except Exception:
            report_fail("zx", "中香网")
            data["zx"] = []

    # 合并存档：只保留本次抓到的站，方便断点/排查
    if os.path.exists(OUT_JSON) and args.only:
        try:
            old = json.load(open(OUT_JSON, encoding="utf-8"))
            old.update(data)
            data = old
        except Exception:
            pass
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    log(f"汇总 → {os.path.basename(OUT_JSON)}（"
        + "，".join(f"{k} {len(v)}" for k, v in data.items()) + "）")

    # 失败标记：CI 的「结果自检」据此把 job 判红（放在推送之后判断，别拦数据）
    if FAILED:
        with open(FAIL_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(FAILED) + "\n")
        log(f"⚠️ {len(FAILED)} 个站失败，已写 {os.path.basename(FAIL_FILE)}："
            + "，".join(x.split(" | ")[0] for x in FAILED))
    elif os.path.exists(FAIL_FILE):
        os.remove(FAIL_FILE)

    if args.dump:
        for k, v in data.items():
            log(f"--- {k} 前 3 条 ---")
            for r in v[:3]:
                print(json.dumps(r, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
