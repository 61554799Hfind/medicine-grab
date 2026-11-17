# -*- coding: utf-8 -*-
r"""
把三个站的供应商数据推到同一个 Google 表格（每天全量覆盖）

表格：sheet_id_supplier.txt 里那一份（工作簿名「供应商_康美中香」）
    · 药通网     ← supplier_data.csv  （supplier_crawl.py 产出，18300 条左右）
    · 康美       ← supplier_km.csv    （supplier_extra_crawl.py / 阿里云函数产出，约 4980 条）
    · 中香       ← supplier_zx.csv    （supplier_extra_crawl.py 产出，约 427 条）
    每一页再加上对应的「XX汇总」页：品名 / 供应商数 / 报价条数 / 最新发布日期 / 主要货源所在地

和 supplier_push.py 的关系
------------------------------------------------------------------
**不改动 supplier_push.py**（那条线每天 08:05 还在跑，是验证过的）。这里 import 它的
`connect / ensure_sheet / build_summary / require_sheet_id` 和表头常量，只是把"一个站"扩成"三个站"。
三个站的表头完全一致，所以筛选、列宽、汇总逻辑都能共用。

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
import csv
import os
import time

import gspread
from gspread.exceptions import APIError

# 复用旧版已经验证过的连接 / 建表 / 汇总逻辑
from supplier_push import (BATCH, CSV_DATE_COL, HEADER, SUM_HEADER,
                           build_summary, connect, ensure_sheet, log,
                           require_sheet_id)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# key, 中文名, 明细页候选名（按顺序找，都没有就建第一个）, CSV, 汇总页候选名
SOURCES = [
    ("ytw", "药通网",   ["药通网", "供应商"],      "supplier_data.csv", ["药通网汇总", "供应商汇总"]),
    ("km",  "康美中药城", ["康美"],                "supplier_km.csv",   ["康美汇总"]),
    ("zx",  "中香网",   ["中香"],                  "supplier_zx.csv",   ["中香汇总"]),
]

WIDTHS = {"供货说明": 300, "手机": 110, "货源所在地": 90, "品名": 90}


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
    """读一个站的 CSV：首列「更新时间」挪到末列「更新日期」。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    if header and header[0] == CSV_DATE_COL:
        catch_days = [(r[0] or day) for r in rows]
        rows = [r[1:] for r in rows]
    else:
        catch_days = [day] * len(rows)
    rows = [r + [d] for r, d in zip(rows, catch_days)]
    # 列数不齐的补空 / 截断，保证和表头一一对应
    n = len(HEADER)
    rows = [(r + [""] * n)[:n] for r in rows]
    real = sorted(set(catch_days)) or [day]
    return rows, (real[0] if len(real) == 1 else f"{real[0]} ~ {real[-1]}")


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

    # ---------- 先把三个 CSV 都读出来，缺文件就记下来跳过 ----------
    plan = []
    for key, name, tabs, fn, sum_tabs in SOURCES:
        if args.only and key != args.only:
            continue
        path = os.path.join(BASE_DIR, fn)
        if not os.path.exists(path):
            log(f"⚠️ {name}：没有 {fn}，这一页跳过（表里保留上一份数据，不会刷白）")
            plan.append((key, name, tabs, sum_tabs, None, None))
            continue
        rows, shown = load_csv(path, day)
        log(f"{name}：{len(rows)} 行，数据日期 {shown}")
        plan.append((key, name, tabs, sum_tabs, rows, shown))

    total = sum(len(p[4]) for p in plan if p[4])
    log(f"合计 {total} 行待写入")

    if args.dry_run:
        for key, name, tabs, sum_tabs, rows, shown in plan:
            if rows is None:
                continue
            s = build_summary(rows)
            top = "，".join(f"{x[0]}({x[1]})" for x in s[:5])
            log(f"  [演练] {name} → {len(rows)} 行 / {len(s)} 个品名；Top5 供应商数：{top}")
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

    for key, name, tabs, sum_tabs, rows, shown in plan:
        if rows is None:
            continue
        # ---------- 明细页 ----------
        ws, tab = find_or_create(sh, tabs, len(rows) + 100, len(HEADER))
        api(sh.batch_update, {"requests": [{"unmergeCells": {"range": {"sheetId": ws.id}}}]})
        api(ws.clear)
        api(ws.update, [HEADER], "A1", value_input_option="RAW")
        written = 0
        for i in range(0, len(rows), BATCH):
            chunk = rows[i:i + BATCH]
            api(ws.update, chunk, f"A{i + 2}", value_input_option="RAW")
            written += len(chunk)
            log(f"  {name} / {tab} 写入 {written}/{len(rows)}")
        log(f"  {name} → 「{tab}」{written} 行完成")

        # ---------- 汇总页 ----------
        summary = build_summary(rows)
        # 中香网不公开手机号 → 去重手机号数恒为 0，写 0 会让人以为"没有供应商"，
        # 所以这一列留空（跟明细页手机列空着是一个意思），排序改成按报价条数。
        if not any((r[7] or "").strip() for r in rows):
            summary = [[s[0], "", s[2], s[3], s[4]] for s in summary]
            summary.sort(key=lambda x: (-x[2], x[0]))
        ws2, tab2 = find_or_create(sh, sum_tabs, len(summary) + 50, len(SUM_HEADER))
        api(sh.batch_update, {"requests": [{"unmergeCells": {"range": {"sheetId": ws2.id}}}]})
        api(ws2.clear)
        api(ws2.update, [SUM_HEADER] + summary, "A1", value_input_option="RAW")
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
                "startColumnIndex": 0, "endColumnIndex": len(HEADER)}}}},
            {"setBasicFilter": {"filter": {"range": {
                "sheetId": ws2.id, "startRowIndex": 0, "endRowIndex": len(summary) + 1,
                "startColumnIndex": 0, "endColumnIndex": len(SUM_HEADER)}}}},
        ]
        for col, px in WIDTHS.items():
            if col in HEADER:
                idx = HEADER.index(col)
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
