# -*- coding: utf-8 -*-
"""
康美中药城「需求信息」抓取 —— 阿里云函数计算 FC 专用（零第三方依赖）

为什么单独写这一份：
    康美 www.kmb2b.com 对境外 IP 的 443 端口直接丢包，GitHub Actions 的
    海外 runner 连列表页都打不开（ConnectTimeout）。国内出口就正常（0.5 秒）。
    所以把康美这一个站放到阿里云 FC（国内出口）上跑，抓完把 CSV 提交回
    GitHub 仓库，再让 Actions 带着这份数据去推 Google 表格。

    为了在 FC 里不折腾依赖，这个文件只用标准库（urllib / http.cookiejar），
    不需要 requests、不需要 cryptography。整个函数就是这一个 .py 文件。

环境变量（在 FC 控制台「函数配置 → 环境变量」里加）：
    KM_USER    康美账号手机号
    KM_PASS    康美密码
    GH_TOKEN   GitHub 细粒度 token，权限：该仓库 Contents = Read and write
    GH_REPO    仓库全名，默认 61554799Hfind/medicine-grab
    GH_BRANCH  分支，默认 main
    DRY_RUN    设 1 则只抓不提交（第一次测试用）

本地测试：
    python km_fc.py --dry-run
"""

import csv
import hashlib
import http.cookiejar
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "https://www.kmb2b.com"
LIST_URL = HOST + "/statichtml/purchase/index.html"
LOGIN_PAGE = HOST + "/html/loginPage.htm"
LOGIN_API = HOST + "/ajaxJson/unionLogin.action"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0")

HEADER = ["品名", "规格", "产地", "数量", "联系电话", "联系人",
          "剩余天数", "有效期", "仓储所在地", "质量要求", "包装要求",
          "资质要求", "认证信息", "会员类型", "经营所在地", "详情链接", "更新日期"]

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"[\s\u00a0\u3000]+")

# FC 的 /code 目录只读，可写的只有 /tmp；本机测试就写在脚本旁边
OUT_CSV = ("/tmp/purchase_km.csv" if os.name != "nt"
           else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "purchase_km_fc.csv"))


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def clean(s):
    if s is None:
        return ""
    s = TAG_RE.sub("", str(s))
    s = (s.replace("&nbsp;", " ").replace("&amp;", "&")
          .replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"'))
    return SPACE_RE.sub(" ", s).strip()


class Client:
    """带 cookie 的最小 HTTP 客户端（标准库实现）。"""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.opener.addheaders = [
            ("User-Agent", UA),
            ("Accept-Language", "zh-CN,zh;q=0.9"),
            ("Referer", LIST_URL),
        ]

    def get(self, url, timeout=30):
        for i in range(3):
            try:
                with self.opener.open(url, timeout=timeout) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                # 康美列表页本身就会返回 404 但正文是正常页面，照读
                body = e.read()
                if e.code < 500 or i == 2:
                    return e.code, body
            except Exception as e:
                if i == 2:
                    raise
                log(f"    重试 {i + 1}/3：{type(e).__name__}")
                time.sleep(2 + i * 3)
        raise RuntimeError("unreachable")


def decode(body):
    m = re.search(rb'charset=["\']?([\w-]+)', body[:3000], re.I)
    enc = m.group(1).decode("ascii", "ignore").lower() if m else "utf-8"
    for cand in (enc, "utf-8", "gb18030"):
        try:
            return body.decode(cand)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", "ignore")


def login(c, user, pwd):
    """康美登录：页面用 $.ajax({dataType:"jsonp"})，jQuery 对 jsonp 强制 GET，
    用 POST 只会拿到通用失败码 0。成功码 1.1 / 1.2。"""
    c.get(LOGIN_PAGE, timeout=25)
    q = urllib.parse.urlencode({
        "loginName": user,
        "passwordcode": hashlib.md5(pwd.encode("utf-8")).hexdigest(),
        "veCode": "",
        "registerPlatform": "",
        "callback": "jsonp1",
    }, quote_via=urllib.parse.quote)
    status, body = c.get(f"{LOGIN_API}?{q}", timeout=30)
    text = decode(body)
    m = re.search(r'returnObject["\']?\s*:\s*["\']?(-?[\d.]+)', text)
    code = m.group(1) if m else ""
    if code in ("1.1", "1.2"):
        log("  登录成功（电话可见）")
        return True
    hint = {"-1": "账号被禁用", "-2": "登录过于频繁", "0": "密码不对"}.get(code, "未知错误")
    log(f"  登录失败：{hint}，returnObject={code or '?'}，原文 {text[:150]}")
    return False


def fetch_rows(c, with_detail=True):
    status, body = c.get(LIST_URL, timeout=30)
    html = decode(body)
    log(f"  列表页 HTTP {status}，{len(html)} 字节")
    cards = re.findall(r'<div class="goods-item">(.*?)</div>\s*</a>', html, re.S)
    if not cards:
        cards = re.findall(r'<div class="goods-item">(.*?)(?=<div class="goods-item">|\Z)',
                           html, re.S)
    rows = []
    for card in cards:
        m = re.search(r'href="([^"]*purchase/info-[^"]+)"', card)
        url = m.group(1) if m else ""
        t = re.search(r'<div class="title-selling-point">(.*?)</div>', card, re.S)
        title = clean(t.group(1)) if t else ""
        days = re.search(r"剩余\s*(\d+)\s*天", title)
        name = SPACE_RE.sub(" ", re.sub(r"剩余\s*\d+\s*天", "", title)).strip()
        fields = {}
        for lab, val in re.findall(
                r'<p class="p-item"><label>([^<]+)</label>\s*(.*?)</p>', card, re.S):
            fields[lab.strip("：: ")] = clean(val)
        row = {
            "品名": name,
            "规格": fields.get("规格", ""),
            "产地": fields.get("产地", ""),
            "数量": fields.get("数量", ""),
            "联系电话": fields.get("电话", ""),
            "联系人": "",
            "剩余天数": days.group(1) if days else "",
            "有效期": "",
            "仓储所在地": "",
            "质量要求": "",
            "包装要求": "",
            "资质要求": "",
            "认证信息": "个人认证" if "res-tag person" in card else "",
            "会员类型": "",
            "经营所在地": "",
            "详情链接": url,
        }
        if with_detail and url:
            try:
                time.sleep(0.2)
                _, dbody = c.get(url, timeout=30)
                d = decode(dbody)
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
                log(f"  ⚠️ 详情 {url} 失败：{type(e).__name__}")
        rows.append(row)
    # 康美没有发布时间 → 剩余天数越大 = 发布越早
    rows.sort(key=lambda r: (-(int(r["剩余天数"]) if r["剩余天数"].isdigit() else -1),
                             r["品名"]))
    today = time.strftime("%Y-%m-%d")
    for r in rows:
        r["更新日期"] = today
    return rows


def gh_request(method, url, token, payload=None, timeout=30):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "km-fc")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def commit_csv(csv_bytes, message):
    """把 CSV 提交到仓库（GitHub Contents API，文件小，一次 PUT 搞定）。"""
    import base64
    repo = os.environ.get("GH_REPO", "61554799Hfind/medicine-grab").strip()
    branch = os.environ.get("GH_BRANCH", "main").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    path = "purchase_km.csv"
    if not token:
        raise RuntimeError("没配 GH_TOKEN，无法提交到仓库")
    api = f"https://api.github.com/repos/{repo}/contents/{path}"
    sha = None
    try:
        st, j = gh_request("GET", f"{api}?ref={branch}", token)
        sha = j.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    # 内容没变就不提交，避免每天一个空 commit
    if sha:
        try:
            st, j = gh_request("GET", f"{api}?ref={branch}", token)
            import base64 as b64
            old = b64.b64decode(j.get("content", "") or "")
            if old == csv_bytes:
                return "内容与仓库一致，跳过提交"
        except Exception:
            pass
    payload = {"message": message, "branch": branch,
               "content": base64.b64encode(csv_bytes).decode()}
    if sha:
        payload["sha"] = sha
    st, j = gh_request("PUT", api, token, payload)
    return f"已提交 {path}（HTTP {st}，commit {str(j.get('commit', {}).get('sha'))[:7]}）"


def run(dry_run=False):
    user = os.environ.get("KM_USER", "").strip()
    pwd = os.environ.get("KM_PASS", "").strip()
    if not user or not pwd:
        return {"ok": False, "msg": "缺环境变量 KM_USER / KM_PASS"}

    c = Client()
    logged = login(c, user, pwd)
    rows = fetch_rows(c, with_detail=True)
    log(f"  康美需求 {len(rows)} 条（{'已登录' if logged else '未登录'}）")
    if not rows:
        return {"ok": False, "msg": "抓到 0 条，康美页面结构可能变了"}

    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=HEADER, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    data = buf.getvalue().encode("utf-8-sig")

    with open(OUT_CSV, "wb") as f:
        f.write(data)

    phones = sum(1 for r in rows if r["联系电话"] and r["联系电话"] != "登录后可见")
    msg = f"{len(rows)} 条，电话可见 {phones} 条"
    if dry_run or os.environ.get("DRY_RUN", "").strip() == "1":
        return {"ok": True, "msg": msg + "（演练，未提交）"}
    msg += "；" + commit_csv(data, f"康美需求自动更新 {rows[0]['更新日期']}")
    return {"ok": True, "msg": msg}


def handler(event, context):
    """FC 事件函数入口。"""
    os.environ.setdefault("TZ", "Asia/Shanghai")
    try:
        time.tzset()
    except Exception:
        pass
    try:
        result = run()
    except Exception as e:
        import traceback
        traceback.print_exc()
        result = {"ok": False, "msg": f"{type(e).__name__}: {e}"}
    log(f"结果：{result}")
    if not result.get("ok"):
        # 让 FC 把这次调用标成失败，方便在监控里看到
        raise RuntimeError(result.get("msg"))
    return json.dumps(result, ensure_ascii=False)


if __name__ == "__main__":
    os.environ.setdefault("TZ", "Asia/Shanghai")
    try:
        time.tzset()
    except Exception:
        pass
    dr = "--dry-run" in sys.argv or os.environ.get("DRY_RUN", "").strip() == "1"
    print(json.dumps(run(dry_run=dr), ensure_ascii=False, indent=1))
