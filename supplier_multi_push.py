# -*- coding: utf-8 -*-
r"""
把三个站的供应商数据推到同一个 Google 表格（每天全量覆盖）

表格：sheet_id_supplier.txt 里那一份
    · 药通网     ← supplier_data.csv  （supplier_crawl.py 产出，18300 条左右）
    · 康美       ← supplier_km.csv    （supplier_extra_crawl.py / 阿里云函数产出，约 4960 条）
    · 中香       ← supplier_zx.csv    （supplier_extra_crawl.py 产出，约 427 条）
    每站再加一张对应的「XX汇总」页。

**表头跟着各站自己的 CSV 走**，不强制统一
------------------------------------------------------------------
药通网是 13 列（品名…实名认证），康美 8 列（品名 规格 产地 货源所在地 数量 电话 详情链接），
中香 12 列（品名 标题 规格 产地 货源所在地 价格 库存 标签 发布时间 有效期天数 截止日期）。
本脚本只做两件事：把各 CSV 首列的「更新时间」挪到末列改名「更新日期」；按自己的列序原样写表。
所以以后任何一个站加减列，都不用改这里。

和 supplier_push.py 的关系
------------------------------------------------------------------
**不改动 supplier_push.py**（那条线每天 08:05 还在跑，是验证过的）。这里只 import 它的
`connect / ensure_sheet / require_sheet_id / log / BATCH`。

设计要点（沿用旧版，都是踩过坑的）
------------------------------------------------------------------
1. 每天全量覆盖：先 unmerge 再 clear 再写 —— clear() **不会**解除合并，不先 unmerge 会导致 A 列只剩左上角。
2. 普通表格：只做「冻结表头 + 表头加粗 + 筛选器 + 列宽」，**不加任何底色**。
3. RAW 写入：手机号保持文本，不会被转成科学计数。
4. 某个站 CSV 不存在（比如阿里云那天没跑成功）→ 跳过该页，**保留上一份数据，不刷白**。
5. 429 限速：整轮约 30~40 次请求，接近每分钟 60 次的上限，撞 429 等 70 秒重试。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe supplier_multi_push.py            # 推三个站
    .venv\Scripts\python.exe supplier_multi_push.py --dry-run  # 只统计不写表
    .venv\Scripts\python.exe supplier_multi_push.py --only zx  # 只推中香
"""
import argparse
import collections
import csv
import os
import time

import gspread
from gspread.exceptions import APIError

# 复用旧版已经验证过的连接 / 建表逻辑
from supplier_push import (BATCH, SUM_HEADER, connect, ensure_sheet, log,
                           require_sheet_id)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_DATE_COL = "更新时间"

# 汇总页表头按各站实际有的字段给：康美没有发布时间，中香没有手机号
KM_SUM = ["品名", "条数", "主要货源所在地"]
ZX_SUM = ["品名", "条数", "最新发布", "主要货源所在地"]

SOURCES = [
    {"key": "ytw", "name": "药通网", "tabs": ["药通网", "供应商"],
     "csv": "supplier_data.csv", "sum_tabs": ["药通网汇总", "供应商汇总"],
     "sum_header": SUM_HEADER},
    {"key": "km", "name": "康美中药城", "tabs": ["康美"],
     "csv": "supplier_km.csv", "sum_tabs": ["康美汇总"],
     "sum_header": KM_SUM},
    {"key": "zx", "name": "中香网", "tabs": ["中香"],
     "csv": "supplier_zx.csv", "sum_tabs": ["中香汇总"],
     "sum_header": ZX_SUM},
]

# 只调宽度，不动颜色
WIDTHS = {"品名": 100, "标题": 170, "规格": 120, "供货说明": 300, "供应说明": 300,
          "手机": 110, "电话": 110, "货源所在地": 100, "产地": 80, "详情链接": 320,
          "价格": 120, "库存": 100, "标签": 140, "截止日期": 100, "发布时间": 100,
          "发布日期": 100, "更新日期": 100}


def api(fn, *a, **kw):
    """429 限速保护：撞了就等 70 秒再试，最多 3 轮。"""
    for i in range(3):
        try:
            return fn(*a, **kw)
        except APIError as e:
            if "429" not in str(e) or i == 2:
                raise
            log(f"  ⚠️ 撞到 429 限速，等 70 秒后重试（第 {i + 1} 次）")
            time.sleep(70)


def load_csv(path, day):
    """读一个站的 CSV：首列「更新时间」挪到末列「更新日期」，列序完全按 CSV 自己来。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    if header and header[0] == CSV_DATE_COL:
        catch_days = [(r[0] or day) for r in rows]
        header = header[1:]
        rows = [r[1:] for r in rows]
    else:
        catch_days = [day] * len(rows)
    rows = [r + [d] for r, d in zip(rows, catch_days)]
    header = header + ["更新日期"]
    # 列数不齐的补空 / 截断，保证和表头一一对应
    n = len(header)
    rows = [(r + [""] * n)[:n] for r in rows]
    real = sorted(set(catch_days)) or [day]
    shown = real[0] if len(real) == 1 else f"{real[0]} ~ {real[-1]}"
    return header, rows, shown


def summarise(header, rows, sum_header):
    """按品名聚合。哪站没有的字段，对应列留空（不编 0）。"""
    idx = {h: i for i, h in enumerate(header)}
    ni = idx.get("品名", 0)
    pi = next((idx[c] for c in ("电话", "手机") if c in idx), None)
    di = next((idx[c] for c in ("发布日期", "发布时间") if c in idx), None)
    li = idx.get("货源所在地")
    by = collections.defaultdict(
        lambda: {"mob": set(), "n": 0, "date": "", "loc": collections.Counter()})
    for r in rows:
        g = by[r[ni]]
        g["n"] += 1
        if pi is not None and r[pi]:
            g["mob"].add(r[pi])
        if di is not None and r[di] > g["date"]:
            g["date"] = r[di]
        if li is not None and r[li]:
            g["loc"][r[li]] += 1

    out = []
    for name, g in by.items():
        line = []
        for h in sum_header:
            if h == "品名":
                line.append(name)
            elif "供应商数" in h:
                line.append(str(len(g["mob"])) if pi is not None else "")
            elif "条数" in h:
                line.append(str(g["n"]))
            elif h.startswith("最新"):
                line.append(g["date"] if di is not None else "")
            elif "货源" in h:
                line.append("、".join(k for k, _ in g["loc"].most_common(3)))
            else:
                line.append("")
        out.append(line)
    # 有「供应商数」的站按供应商数排，没有的按条数排；都用条数做次级排序
    ci = next((i for i, h in enumerate(sum_header) if "条数" in h), None)
    si = next((i for i, h in enumerate(sum_header) if "供应商数" in h), None)

    def key(x):
        sup = x[si] if si is not None else ""
        cnt = x[ci] if ci is not None else "0"
        return (-(int(sup) if sup.isdigit() else 0), -int(cnt), x[0])

    out.sort(key=key)
    return out


def find_or_create(sh, candidates, rows, cols):
    """按候选名找已有工作表；都没有就新建第一个候选名。"""
    for name in candidates:
        try:
            return sh.worksheet(name), name
        except gspread.WorksheetNotFound:
            continue
    return ensure_sheet(sh, candidates[0], rows, cols), candidates[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只统计不写表")
    ap.add_argument("--only", choices=["ytw", "km", "zx"], help="只推一个站")
    args = ap.parse_args()
    day = time.strftime("%Y-%m-%d")

    # ---------- 先把 CSV 都读出来，缺文件就记下来跳过 ----------
    plan = []
    for src in SOURCES:
        if args.only and src["key"] != args.only:
            continue
        path = os.path.join(BASE_DIR, src["csv"])
        if not os.path.exists(path):
            log(f"⚠️ {src['name']}：没有 {src['csv']}，这一页跳过（表里保留上一份数据，不会刷白）")
            plan.append((src, None, None, None))
            continue
        header, rows, shown = load_csv(path, day)
        log(f"{src['name']}：{len(rows)} 行 × {len(header)} 列，数据日期 {shown}"
            f"  [{' / '.join(header)}]")
        plan.append((src, header, rows, shown))

    total = sum(len(p[2]) for p in plan if p[2])
    log(f"合计 {total} 行待写入")

    if args.dry_run:
        for src, header, rows, shown in plan:
            if rows is None:
                continue
            s = summarise(header, rows, src["sum_header"])
            top = "，".join(f"{x[0]}({x[1] or x[-1]})" for x in s[:5])
            log(f"  [演练] {src['name']} → {len(rows)} 行 / {len(s)} 个品名；Top5：{top}")
        log("--dry-run，不写表")
        return

    gc, sa_email = connect()
    sid = require_sheet_id()
    try:
        sh = gc.open_by_key(sid)
    except gspread.SpreadsheetNotFound:
        raise SystemExit(
            f"服务账号 {sa_email} 打不开表格 {sid}\n"
            "→ 请在 Google 表格里点「共享」，把这个邮箱加成「编辑者」。")
    log(f"打开表格「{sh.title}」")

    bold = {"textFormat": {"bold": True}}
    fmt_reqs = []
    done = []

    for src, header, rows, shown in plan:
        if rows is None:
            continue
        name, sum_header = src["name"], src["sum_header"]
        # ---------- 明细页 ----------
        ws, tab = find_or_create(sh, src["tabs"], len(rows) + 100, len(header))
        api(sh.batch_update, {"requests": [{"unmergeCells": {"range": {"sheetId": ws.id}}}]})
        api(ws.clear)
        api(ws.update, [header], "A1", value_input_option="RAW")
        written = 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            api(ws.update, chunk, f"A{i + 2}", value_input_option="RAW")
            written += len(chunk)
            log(f"  {name} / {tab} 写入 {written}/{len(rows)}")
        log(f"  {name} → 「{tab}」{written} 行完成")

        # ---------- 汇总页 ----------
        summary = summarise(header, rows, sum_header)
        ws2, tab2 = find_or_create(sh, src["sum_tabs"], len(summary) + 50, len(sum_header))
        api(sh.batch_update, {"requests": [{"unmergeCells": {"range": {"sheetId": ws2.id}}}]})
        api(ws2.clear)
        api(ws2.update, [sum_header] + summary, "A1", value_input_option="RAW")
        log(f"  {name} → 「{tab2}」{len(summary)} 行汇总完成")

        # ---------- 格式（统一收集，最后一次性下发） ----------
        fmt_reqs += [
            {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": bold},
                            "fields": "userEnteredFormat.textFormat"}},
            {"repeatCell": {"range": {"sheetId": ws2.id, "startRowIndex": 0, "endRowIndex": 1},
                            "cell": {"userEnteredFormat": bold},
                            "fields": "userEnteredFormat.textFormat"}},
            {"updateSheetProperties": {"properties": {
                "sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"updateSheetProperties": {"properties": {
                "sheetId": ws2.id, "gridProperties": {"frozenRowCount": 1}},
                "fields": "gridProperties.frozenRowCount"}},
            {"setBasicFilter": {"filter": {"range": {
                "sheetId": ws.id, "startRowIndex": 0, "endRowIndex": len(rows) + 1,
                "startColumnIndex": 0, "endColumnIndex": len(header)}}}},
            {"setBasicFilter": {"filter": {"range": {
                "sheetId": ws2.id, "startRowIndex": 0, "endRowIndex": len(summary) + 1,
                "startColumnIndex": 0, "endColumnIndex": len(sum_header)}}}},
        ]
        for col, px in WIDTHS.items():
            if col in header:
                idx = header.index(col)
                fmt_reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                              "startIndex": idx, "endIndex": idx + 1},
                    "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        done.append(f"{tab}({len(rows)})")
        time.sleep(0.5)

    if fmt_reqs:
        try:
            api(sh.batch_update, {"requests": fmt_reqs})
            log("格式完成（冻结表头 / 表头加粗 / 筛选器 / 列宽；无底色）")
        except Exception as e:
            log(f"⚠️ 格式设置失败（数据已写入，不影响内容）：{type(e).__name__}: {e}")

    log(f"全部完成：{'、'.join(done)} → https://docs.google.com/spreadsheets/d/{sid}/edit")


if __name__ == "__main__":
    main()
