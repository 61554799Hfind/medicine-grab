# -*- coding: utf-8 -*-
r"""
康美药材价格 —— 阿里云函数计算 FC 专用入口（零第三方依赖）

为什么单独一个函数：
    价格数据在 www.kmzyw.com.cn（康美中药网）上，这个域名和康美供求那个 kmb2b.com 一样，
    对境外 IP 不通 —— GitHub Actions 的海外 runner 三个请求全部 30 秒超时，实测过。
    所以只能在国内出口（阿里云 FC）上抓，抓完把文件提交回 GitHub 仓库，
    再由 GitHub Actions 拿去推 Google 表格。

为什么不并进原来的 km_fc：
    原函数抓「康美需求 + 康美供货 249 页」已经用到 5 分钟上下，
    这一份还要再 5 分钟（111 次翻页 + 991 次月价格请求，月价格是 6 并发抓的），
    并在一起会撞 600 秒执行超时上限，一超时两边都拿不到数据。
    分成两个函数互不影响：价格挂了不影响供货，供货挂了也不影响价格。

环境变量（FC 控制台 → 函数配置 → 环境变量）：
    GH_TOKEN   GitHub 细粒度 token，权限：该仓库 Contents = Read and write
    GH_REPO    仓库全名，默认 61554799Hfind/medicine-grab
    GH_BRANCH  分支，默认 main
    DRY_RUN    设 1 只抓不提交
    MAX_PAGES  只翻前 N 页（测试用，正常别设）
    WORKERS    月价格并发数，默认 6（实测 6 并发跑完约 3 分半）

产出（提交回仓库，供 spice-daily.yml 推表）：
    km_spice_price.csv      991 行明细（medicine_list.txt 对照康美报价，
                            六市场价 + 周月季年涨跌 + 历史价格列 + 更新时间）
    km_spice_hist.json.gz   991 味的月价格（约 35 个月），压缩后约 95KB

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

BUILD = "v6-spice 2026-11-18 按medicine_list全量991行"


class GhError(RuntimeError):
    """GitHub API 错误：带上方法、URL、状态码和响应体前 300 字"""

    def __init__(self, method, url, code, body=""):
        self.method, self.url, self.code, self.body = method, url, code, body
        RuntimeError.__init__(self, "%s %s → HTTP %s %s"
                              % (method, url, code, (body or "").replace("\n", " ")[:300]))

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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read()[:400].decode("utf-8", "replace")
        except Exception:
            pass
        raise GhError(method, url, e.code, body) from None


DEFAULT_REPO = "61554799Hfind/medicine-grab"


def gh_conf():
    """读 GH_REPO / GH_BRANCH / GH_TOKEN 并做格式校验

    踩过的坑：把 token 填进了 GH_REPO，URL 就变成
    /repos/github_pat_xxxx/contents/... → 一律 404，看起来像「文件不存在 / 没权限」，
    其实只是变量填错了。所以这里先自己检查一遍，错得明明白白。
    """
    repo = (os.environ.get("GH_REPO", "").strip().rstrip("/")
            or DEFAULT_REPO)
    branch = os.environ.get("GH_BRANCH", "main").strip() or "main"
    token = os.environ.get("GH_TOKEN", "").strip()

    if repo.startswith(("ghp_", "github_pat_", "gho_", "ghu_", "ghs_", "ghr_")):
        raise RuntimeError(
            "GH_REPO 填成 token 了（当前值以 %s 开头）：GH_REPO 要填「用户名/仓库名」= %s，"
            "token 只放 GH_TOKEN 那一条" % (repo[:14], DEFAULT_REPO))
    if repo.count("/") != 1 or " " in repo:
        raise RuntimeError("GH_REPO 格式不对（当前值 = %r）：应为 owner/repo，例如 %s"
                           % (repo, DEFAULT_REPO))
    if not token:
        raise RuntimeError("没配 GH_TOKEN，无法提交到仓库")
    if token.startswith("ghp_") or token.startswith("github_pat_"):
        pass
    else:
        raise RuntimeError("GH_TOKEN 看起来不是 GitHub token（应以 ghp_ 或 github_pat_ 开头，"
                           "当前值前 6 位 = %r）" % token[:6])
    return repo, branch, token


def commit_file(data, message, path):
    """把文件提交到仓库（Contents API）。内容没变就跳过，避免每天一个空 commit。"""
    repo, branch, token = gh_conf()
    api = "https://api.github.com/repos/%s/contents/%s" % (repo, path)
    sha, old = None, None
    try:
        st, j = gh_request("GET", "%s?ref=%s" % (api, branch), token)
        sha = j.get("sha")
        old = base64.b64decode(j.get("content") or "")
    except GhError as e:
        if e.code != 404:
            raise
        # 404 有两种可能：文件确实还没进过仓库（正常，下面 PUT 新建），
        # 或者 token 根本没有这个仓库的权限（GitHub 对无权限的仓库统一回 404）。
        log("  读取 %s 返回 404，按新建处理" % path)
    if old == data:
        return "内容与仓库一致，跳过提交"
    payload = {"message": message, "branch": branch,
               "content": base64.b64encode(data).decode()}
    if sha:
        payload["sha"] = sha
    try:
        st, j = gh_request("PUT", api, token, payload)
    except GhError as e:
        hint = ""
        if e.code in (403, 404):
            hint = ("｜仓库 = %s，分支 = %s。若这个值正确，说明 GH_TOKEN 没有该仓库的 Contents 读写权限"
                    "（GitHub 对无权仓库回 404 而不是 403）：GitHub → Settings → Developer settings → "
                    "Personal access tokens → 选中该 token → Repository access 勾上 medicine-grab → "
                    "Permissions → Contents = Read and write" % (repo, branch))
        raise RuntimeError("提交 %s 失败：%s%s" % (path, e, hint)) from None
    return "已提交 %s（HTTP %s，commit %s）" % (
        path, st, str(j.get("commit", {}).get("sha"))[:7])


def diag_endpoints(row=None):
    """失败时快速探一遍四个接口，把状态码写进错误信息，省得下次再猜是哪一步挂的"""
    k = km_spice.Kmzyw()
    out = []
    for label, url in (("今日价格 today_price.html", km_spice.URL_PRICE),
                       ("涨跌排行 price_ranking.html", km_spice.URL_RANK)):
        try:
            k._open(url, timeout=20, retries=1)
            out.append("%s=200" % label)
        except Exception as e:
            out.append("%s=%s" % (label, e))
    name, standard, origin = row or ("", "", "")
    if not name:                      # 没有现成样本就从列表页第一行取一个真名字
        try:
            got = k.parse_rows(k._open(km_spice.URL_PRICE, timeout=20, retries=1))
            if got and len(got[0]) >= 3:
                name, standard, origin = got[0][0], got[0][1], got[0][2]
        except Exception:
            pass
    for label, fn in (("逐日走势 todaylist.jsp", k.history),
                      ("月价格 list.jsp", k.months)):
        if not name:
            out.append("%s=跳过" % label)
            continue
        try:
            out.append("%s=200(%d条)" % (label, len(fn(name, standard, origin) or [])))
        except Exception as e:
            out.append("%s=%s" % (label, e))
    return "；".join(out)


def first_row():
    """从已写出的 CSV 里取一行做诊断样本（抓取失败了就返回 None）"""
    try:
        import csv
        with open(_OUT_CSV, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                return r["品名"], r["规格"], r["产地"]
    except Exception:
        pass
    return None


def run(dry_run=False):
    max_pages = os.environ.get("MAX_PAGES", "").strip()
    max_pages = int(max_pages) if max_pages.isdigit() else None
    workers = os.environ.get("WORKERS", "").strip()
    workers = int(workers) if workers.isdigit() else 6

    t0 = time.time()
    try:
        rows, hist, stats = km_spice.crawl(max_pages=max_pages, workers=workers)
    except Exception as e:
        raise RuntimeError("抓取康美失败：%s: %s" % (type(e).__name__, e)) from None
    if not rows:
        return {"ok": False, "msg": "药材 0 行，康美中药网页面结构可能变了"}

    try:
        km_spice.write_files(rows, hist, _OUT_CSV, False)   # 只写 CSV，月价格自己压 gz
        csv_bytes = open(_OUT_CSV, "rb").read()
        gz_bytes = gzip.compress(
            json.dumps(hist, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 9)
        with open(_OUT_GZ, "wb") as f:
            f.write(gz_bytes)
    except Exception as e:
        raise RuntimeError("写文件失败：%s: %s" % (type(e).__name__, e)) from None

    up = sum(1 for r in rows if r["走势"] == "涨")
    down = sum(1 for r in rows if r["走势"] == "跌")
    msg = ("药材 %d 行（%d 个品名，对照 medicine_list %d 味；周涨 %d / 周跌 %d）；"
           "有月价格 %d 味；耗时 %ds"
           % (stats["herb_rows"], stats["herb_names"], stats["med_list"], up, down,
              stats.get("with_month", 0), time.time() - t0))
    if stats.get("no_month"):
        msg += "；无月价格 %d 味" % len(stats["no_month"])

    if dry_run or os.environ.get("DRY_RUN", "").strip() == "1":
        return {"ok": True, "msg": msg + "（演练，未提交）"}

    today = time.strftime("%Y-%m-%d")
    msg += "；" + commit_file(csv_bytes, "康美药材价格自动更新 %s" % today,
                              "km_spice_price.csv")
    msg += "；" + commit_file(gz_bytes, "康美药材月价格自动更新 %s" % today,
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
        msg = "%s: %s" % (type(e).__name__, e)
        try:
            msg += " ｜ 接口诊断：" + diag_endpoints(first_row())
        except Exception as e2:
            msg += " ｜ 接口诊断失败：%s" % e2
        result = {"ok": False, "msg": msg}
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
