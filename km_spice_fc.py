# -*- coding: utf-8 -*-
r"""
康美香料价格 —— 阿里云函数计算 FC 专用入口（零第三方依赖）

为什么单独一个函数：
    香料价格在 www.kmzyw.com.cn（康美中药网）上，这个域名和康美供求那个 kmb2b.com 一样，
    对境外 IP 不通 —— GitHub Actions 的海外 runner 三个请求全部 30 秒超时，实测过。
    所以只能在国内出口（阿里云 FC）上抓，抓完把文件提交回 GitHub 仓库，
    再由 GitHub Actions 拿去推 Google 表格。

为什么不并进原来的 km_fc：
    原函数抓「康美需求 + 康美供货 249 页」已经用到 5 分钟上下，
    香料这一份还要再 2~3 分钟（111 次翻页 + 46 次走势请求），
    并在一起会撞 600 秒执行超时上限，一超时两边都拿不到数据。
    分成两个函数互不影响：香料挂了不影响供货，供货挂了也不影响香料。

环境变量（FC 控制台 → 函数配置 → 环境变量）：
    GH_TOKEN   GitHub 细粒度 token，权限：该仓库 Contents = Read and write
    GH_REPO    仓库全名，默认 61554799Hfind/medicine-grab
    GH_BRANCH  分支，默认 main
    DRY_RUN    设 1 只抓不提交
    MAX_PAGES  只翻前 N 页（测试用，正常别设）

产出（提交回仓库，供 spice-daily.yml 推表）：
    km_spice_price.csv      46 行香料明细（六市场价 + 周月季年涨跌）
    km_spice_hist.json.gz   46 味近一年逐日走势（压缩后约 39KB）

本地测试：
    python km_spice_fc.py --dry-run
"""

import base64
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request

import km_spice

BUILD = "v1-spice 2026-11-18"

# FC 的 /code 只读，可写只有 /tmp；本机测试写在脚本旁边
_LOCAL = os.path.dirname(os.path.abspath(__file__))
_OUT_CSV = ("/tmp/km_spice_price.csv" if os.name != "nt"
            else os.path.join(_LOCAL, "_km_spice_price_fc.csv"))
_OUT_GZ = ("/tmp/km_spice_hist.json.gz" if os.name != "nt"
           else os.path.join(_LOCAL, "_km_spice_hist_fc.json.gz"))


def log(msg=""):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def gh_request(method, url, token, payload=None, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "km-spice-fc")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode() or "{}")


def commit_file(data, message, path):
    """把文件提交到仓库（Contents API）。内容没变就跳过，避免每天一个空 commit。"""
    repo = os.environ.get("GH_REPO", "61554799Hfind/medicine-grab").strip()
    branch = os.environ.get("GH_BRANCH", "main").strip()
    token = os.environ.get("GH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("没配 GH_TOKEN，无法提交到仓库")
    api = "https://api.github.com/repos/%s/contents/%s" % (repo, path)
    sha, old = None, None
    try:
        st, j = gh_request("GET", "%s?ref=%s" % (api, branch), token)
        sha = j.get("sha")
        old = base64.b64decode(j.get("content") or "")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    if old == data:
        return "内容与仓库一致，跳过提交"
    payload = {"message": message, "branch": branch,
               "content": base64.b64encode(data).decode()}
    if sha:
        payload["sha"] = sha
    st, j = gh_request("PUT", api, token, payload)
    return "已提交 %s（HTTP %s，commit %s）" % (
        path, st, str(j.get("commit", {}).get("sha"))[:7])


def run(dry_run=False):
    max_pages = os.environ.get("MAX_PAGES", "").strip()
    max_pages = int(max_pages) if max_pages.isdigit() else None

    t0 = time.time()
    rows, hist, stats = km_spice.crawl(max_pages=max_pages)
    if not rows:
        return {"ok": False, "msg": "香料 0 行，康美中药网页面结构可能变了"}

    km_spice.write_files(rows, hist, _OUT_CSV, False)   # 只写 CSV，走势自己压 gz
    csv_bytes = open(_OUT_CSV, "rb").read()
    gz_bytes = gzip.compress(
        json.dumps(hist, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
    with open(_OUT_GZ, "wb") as f:
        f.write(gz_bytes)

    up = sum(1 for r in rows if r["走势"] == "涨")
    down = sum(1 for r in rows if r["走势"] == "跌")
    msg = ("香料 %d 行（%d 味，周涨 %d / 周跌 %d）；走势 %d 味；耗时 %ds"
           % (stats["spice_rows"], stats["spice_names"], up, down,
              stats["with_hist"], time.time() - t0))
    if stats["no_hist"]:
        msg += "；无走势：" + "、".join(k.split("|")[0] for k in stats["no_hist"][:5])

    if dry_run or os.environ.get("DRY_RUN", "").strip() == "1":
        return {"ok": True, "msg": msg + "（演练，未提交）"}

    today = time.strftime("%Y-%m-%d")
    msg += "；" + commit_file(csv_bytes, "康美香料价格自动更新 %s" % today,
                              "km_spice_price.csv")
    msg += "；" + commit_file(gz_bytes, "康美香料走势自动更新 %s" % today,
                              "km_spice_hist.json.gz")
    return {"ok": True, "msg": msg}


def handler(event, context):
    """FC 事件函数入口（入口填 index.handler）。"""
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
        result = {"ok": False, "msg": "%s: %s" % (type(e).__name__, e)}
    result.setdefault("build", BUILD)
    log("结果：%s" % result)
    if not result.get("ok"):
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
