# -*- coding: utf-8 -*-
"""用接口重跑 CSV 里已有的药材，逐行对比，验证接口方案与浏览器方案结果是否一致。
   输出实时写入 _validate.log（增量），异常也写进去。"""
import csv
import random
import re
import sys
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import requests

LOG = r"D:\medicine_grab\_validate.log"
BASE = "https://www.yt1998.com/price/nowDayPriceQ!getZhangLuo.do"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
    "Referer": "https://www.yt1998.com/priceUpDown.html",
    "X-Requested-With": "XMLHttpRequest",
}
PERIODS = [("yesterday", "昨日对比"), ("zhouduibi", "周对比"), ("yueduibi", "月对比"),
           ("jiduibi", "季度对比"), ("nianduibi", "年对比")]
TAG = re.compile(r"<[^>]+>")

_fh = open(LOG, "w", encoding="utf-8")
_lock = threading.Lock()
_local = threading.local()


def log(m=""):
    with _lock:
        _fh.write(str(m) + "\n")
        _fh.flush()


def strip_tag(s):
    return TAG.sub("", s or "").strip()


def session():
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        _local.s = s
    return s


def fetch(name, period, page_size=200, tries=3):
    params = {"random": random.random(), "ascFlag": "", "ycnam": name, "market": "1",
              "leibie": "", "istoday": "0", "spices": "", "tea": "",
              "paramName": period, "paramValue": 1, "pageIndex": 0, "pageSize": page_size}
    url = BASE + "?" + urlencode(params, safe="")
    for i in range(tries):
        try:
            r = session().get(url, headers=HEADERS, timeout=20)
            if r.status_code == 200:
                j = r.json()
                if isinstance(j, dict) and "data" in j:
                    return j.get("data") or []
        except Exception as e:
            log(f"    [重试{i+1}] {name}/{period}: {e!r}")
        time.sleep(0.4 * (i + 1))
    return None


def build(name):
    base, order = OrderedDict(), []
    for period, key in PERIODS:
        rows = fetch(name, period)
        if rows is None:
            log(f"  ! {name} {period} 三次均失败")
            continue
        if not rows:
            continue
        cur = {}
        for d in rows:
            spec = (d.get("guige") or "").strip()
            origin = (d.get("chandi") or "").strip()
            k = f"{spec}_{origin}"
            cur[k] = strip_tag(d.get("zhangluo"))
            if k not in base:
                base[k] = {"规格": spec, "产地": origin,
                           "原价": (d.get("price") or "").strip(),
                           "现价(元/kg)": (d.get("pri") or "").strip(),
                           "走势": (d.get("zoushi") or "").strip()}
                order.append(k)
        for k, v in cur.items():
            if k in base:
                base[k][key] = v
    return [[name, base[k].get("规格", ""), base[k].get("产地", ""), base[k].get("原价", ""),
             base[k].get("现价(元/kg)", ""), base[k].get("走势", ""), base[k].get("昨日对比", ""),
             base[k].get("周对比", ""), base[k].get("月对比", ""), base[k].get("季度对比", ""),
             base[k].get("年对比", "")] for k in order]


def main():
    with open(r"D:\medicine_grab\药材涨落对比结果.csv", "r", encoding="utf-8-sig") as f:
        old = list(csv.reader(f))[1:]
    old_map = {}
    for r in old:
        old_map.setdefault(r[0], []).append(tuple(r[1:11]))

    names = list(old_map.keys())
    log(f"旧 CSV：{len(old_map)} 个药材 / {len(old)} 行。开始用接口重跑核对……")
    t0 = time.time()
    done = [0]

    def wrap(n):
        r = build(n)
        done[0] += 1
        if done[0] % 50 == 0:
            log(f"  进度 {done[0]}/{len(names)}  用时 {time.time()-t0:.0f}s")
        return r

    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(zip(names, ex.map(wrap, names)))
    elapsed = time.time() - t0

    diff_med, diff_rows, missing, examples = [], 0, 0, []
    for name, rows in results:
        new_t = [tuple(r[1:11]) for r in rows]
        old_t = old_map[name]
        if not new_t:
            missing += 1
            diff_med.append((name, "接口返回空"))
            continue
        if sorted(new_t) != sorted(old_t):
            diff_rows += len(set(old_t) ^ set(new_t))
            diff_med.append((name, f"旧{len(old_t)}行 / 新{len(new_t)}行"))
            if len(examples) < 10:
                examples.append((name, list(set(old_t) - set(new_t))[:3],
                                 list(set(new_t) - set(old_t))[:3]))

    log(f"\n接口重跑耗时：{elapsed:.1f}s  （{len(names)} 药材 × 5 周期 = {len(names)*5} 次请求）")
    log(f"平均 {elapsed/(len(names)*5):.3f}s / 请求")
    log(f"\n完全一致：{len(names)-len(diff_med)} / {len(names)} 个药材")
    log(f"有差异：{len(diff_med)} 个（接口返回空 {missing} 个），差异行约 {diff_rows}")
    if diff_med:
        log("\n--- 差异药材（前 40）---")
        for n, why in diff_med[:40]:
            log(f"  {n}: {why}")
    if examples:
        log("\n--- 样例对比 ---")
        for name, oo, nn in examples:
            log(f"  【{name}】")
            log(f"    仅旧: {oo}")
            log(f"    仅新: {nn}")
    log("\n=== 校验结束 ===")


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        log("=== TRACEBACK ===\n" + traceback.format_exc())
        sys.exit(1)
    finally:
        _fh.close()
