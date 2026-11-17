# -*- coding: utf-8 -*-
r"""
价格线「今天已经抓成功过，就别再抓一遍」的开关。

背景（实测数据，不是猜的）：
    GitHub 的 schedule（cron）在这个仓库上长期迟到 6~8 小时。
    cron 写的是 UTC 00:17（北京 08:17），实际启动时间是北京 14:10~15:51。
    于是同一天会跑两遍：
        · Cloudflare / 手动 dispatch → 早上就正常跑完
        · GitHub 自己的 cron 迟到下午 → 又跑一遍 20 分钟抓取 + 十几分钟建图
    这个脚本把「最近一次成功完成的日期 + 行数」写进仓库根目录 crawl_state.json，
    下一次运行先读它：当天已经成功过（且行数达标）就直接跳过。

用法：
    python ci_state.py check                      # 打印 skip=true/false，写进 GITHUB_OUTPUT
    python ci_state.py save --rows 5367 --meds 971  # 成功后记录当天已完成

    FORCE=1 python ci_state.py check              # 强制重抓（手动补抓时用）
    GH_TOKEN / GITHUB_TOKEN                      # 写状态要 token；只读不用（公开仓库）

判定规则（三条同时满足才跳过）：
    1. state.date == 今天（按北京时间算，不看 UTC）
    2. state.rows >= MIN_ROWS（默认 300，防止「只抓到几条」也被当成完成）
    3. 没有设 FORCE
"""

import argparse
import base64
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CN = timezone(timedelta(hours=8))          # 北京时间，不用管 runner 的 UTC
REPO = (os.environ.get("GITHUB_REPOSITORY") or "61554799Hfind/medicine-grab").strip()
BRANCH = (os.environ.get("GH_BRANCH") or "main").strip()
STATE_FILE = "crawl_state.json"
MIN_ROWS = int(os.environ.get("MIN_ROWS") or "300")


def log(msg=""):
    print(msg, flush=True)


def today_cn():
    return datetime.now(CN).strftime("%Y-%m-%d")


def token():
    return (os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or "").strip()


def gh_api(path, method="GET", body=None, timeout=25):
    """调 GitHub Contents API。公开仓库只读可以不传 token。"""
    url = "https://api.github.com/repos/%s%s" % (REPO, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    heads = {
        "User-Agent": "medicine-crawl-state",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    tk = token()
    if tk:
        heads["Authorization"] = "Bearer %s" % tk
    req = urllib.request.Request(url, data=data, method=method, headers=heads)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", errors="replace")
        return r.status, (json.loads(raw) if raw.strip() else {})


def read_local():
    """checkout 出来的那份就是最新 main，先读本地，省一次网络请求。"""
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log("本地 %s 读不了：%r（当作没有）" % (STATE_FILE, e))
        return None


def read_remote():
    """本地没有（第一次跑 / 文件被删）再问一次 API，顺便防 checkout 落后。"""
    try:
        _, d = gh_api("/contents/%s?ref=%s" % (STATE_FILE, BRANCH))
        content = base64.b64decode(d.get("content") or "").decode("utf-8")
        return json.loads(content), d.get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log("读远端状态失败 HTTP %s" % e.code)
    except Exception as e:
        log("读远端状态失败 %r" % (e,))
    return None, None


def write_output(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in kv.items():
            f.write("%s=%s\n" % (k, v))


def write_summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def cmd_check(_args):
    force = (os.environ.get("FORCE") or "").strip().lower() in ("1", "true", "yes", "y")
    d = today_cn()

    st = read_local()
    if st is None:
        st, _ = read_remote()

    sdate = (st or {}).get("date") or ""
    srows = int((st or {}).get("rows") or 0)
    smeds = (st or {}).get("meds") or ""
    fresh = sdate == d and srows >= MIN_ROWS
    skip = fresh and not force

    log("今天（北京）：%s" % d)
    log("上次成功记录：%s，%s 行%s" % (sdate or "无", srows or 0,
                                 ("，%s 味" % smeds) if smeds else ""))
    log("判定：%s" % ("今天已抓成功 → 本次跳过抓取" if skip else
                    ("今天还没抓成功 → 正常抓取" if not fresh else "FORCE 已设 → 强制重抓")))

    write_output(skip="true" if skip else "false", date=d, state_date=sdate,
                 state_rows=str(srows))
    write_summary([
        "### 价格线：今天是否已经抓过",
        "",
        "| 项 | 值 |",
        "|---|---|",
        "| 今天（北京） | %s |" % d,
        "| 上次成功 | %s（%s 行） |" % (sdate or "无记录", srows or 0),
        "| 本次动作 | %s |" % ("跳过（今天已抓成功）" if skip else "正常抓取"),
        "",
    ])
    return 0


def cmd_save(args):
    d = today_cn()
    state = {
        "date": d,
        "rows": int(args.rows),
        "meds": int(args.meds),
        "updated": datetime.now(CN).strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": os.environ.get("GITHUB_RUN_ID", ""),
        "run_url": ("%s/%s/actions/runs/%s" % (
            os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
            REPO, os.environ.get("GITHUB_RUN_ID", ""))) if os.environ.get("GITHUB_RUN_ID") else "",
    }
    if not token():
        log("没有 GH_TOKEN/GITHUB_TOKEN，跳过写状态（只在本地跑时会这样）")
        return 0

    body_tpl = {
        "message": "价格线状态 %s（%d 行，%d 味）" % (d, state["rows"], state["meds"]),
        "content": base64.b64encode(
            json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii"),
        "branch": BRANCH,
    }
    last = ""
    for i in range(1, 4):
        sha = None
        try:
            _, cur = gh_api("/contents/%s?ref=%s" % (STATE_FILE, BRANCH))
            sha = cur.get("sha")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                last = "读 sha HTTP %s" % e.code
        except Exception as e:
            last = repr(e)

        body = dict(body_tpl)
        if sha:
            body["sha"] = sha
        try:
            code, _ = gh_api("/contents/%s" % STATE_FILE, method="PUT", body=body)
            log("已写入 %s（HTTP %s）：%s" % (STATE_FILE, code,
                                          json.dumps(state, ensure_ascii=False)))
            return 0
        except urllib.error.HTTPError as e:
            last = "HTTP %s %s" % (e.code, e.read().decode("utf-8", errors="replace")[:200])
        except Exception as e:
            last = repr(e)
        log("第 %d 次写状态失败：%s" % (i, last))
        time.sleep(2 * i)

    log("写状态连续失败，不影响本次数据（明天会当成没抓过，多抓一次而已）：%s" % last)
    return 0


def main():
    ap = argparse.ArgumentParser(description="价格线当日完成状态")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="判断今天是否已经抓成功")
    p = sub.add_parser("save", help="记录今天已抓成功")
    p.add_argument("--rows", type=int, required=True, help="本次写入 CSV 的数据行数")
    p.add_argument("--meds", type=int, default=0, help="成功抓到数据的药材味数")
    args = ap.parse_args()
    return cmd_check(args) if args.cmd == "check" else cmd_save(args)


if __name__ == "__main__":
    raise SystemExit(main())
