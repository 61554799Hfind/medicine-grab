# -*- coding: utf-8 -*-
r"""
把 supplier_crawl.py 抓到的供应商数据推到 Google 表格（每天全量覆盖）

表格：sheet_id_supplier.txt 里那一份（工作簿名「供应商_ytw」）
    · 工作表「供应商」
        第 1 行  标题：数据日期 + 总条数 + 覆盖品名数
        第 2 行  表头
        第 3 行起 明细（按品名 / 发布日期倒序，带筛选器）
    · 工作表「供应商汇总」  —— 每味药材的供应商数 / 报价条数 / 最新发布日期 / 主要货源地

设计要点
------------------------------------------------------------------
1. **每天全量覆盖**：先 clear() 再写，行数变少也不会留旧数据。
2. **先解除合并**：原表 A 列有过合并单元格（clear 不会解除合并，
   只有左上角保留值，其余读出来是空），所以写入前强制 unmerge 整表。
   本表全程不合并单元格。
3. **RAW 写入**：手机号保持文本（不会被转数字 / 科学计数），
   日期是 ISO 文本，A→Z 排序等价于按时间排序。
4. **配额**：分批 5000 行，一次全量约 10 次请求（限额 60/分钟）。

用法
------------------------------------------------------------------
    .venv\\Scripts\\python.exe supplier_push.py            # 用 supplier_data.csv
    .venv\\Scripts\\python.exe supplier_push.py --dry-run  # 只看统计不写表
"""
import argparse
import collections
import csv
import os
import time

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_CSV = os.path.join(BASE_DIR, "supplier_data.csv")
SA_FILE = os.path.join(BASE_DIR, "service_account.json")
SHEET_ID_FILE = os.path.join(BASE_DIR, "sheet_id_supplier.txt")

MAIN_SHEET = "供应商"
SUM_SHEET = "供应商汇总"
# CSV 第一列是「更新时间」（全表同一天，写进表格纯属噪音，落表时丢掉，改放标题行）
CSV_DATE_COL = "更新时间"
HEADER = ["品名", "规格", "产地", "货源所在地", "单价",
          "数量", "联系人", "手机", "供货说明", "发布日期", "剩余天数", "实名认证"]
SUM_HEADER = ["品名", "供应商数", "报价条数", "最新发布日期", "主要货源所在地"]
BATCH = 5000
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BROWN = {"red": 0.36, "green": 0.24, "blue": 0.14}
WHITE = {"red": 1, "green": 1, "blue": 1}


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def require_sheet_id():
    sid = os.environ.get("SHEET_ID_SUPPLIER", "").strip()
    if not sid and os.path.exists(SHEET_ID_FILE):
        sid = open(SHEET_ID_FILE, encoding="utf-8").read().strip()
    if not sid:
        raise SystemExit(
            "找不到供应商表格 ID。\n"
            f"  请把表格 ID 写进 {SHEET_ID_FILE}（一行），或设环境变量 SHEET_ID_SUPPLIER。")
    return sid


def connect():
    # 本机开着 VPN 时，系统代理变量会让请求绕远路；直连更快
    if not os.environ.get("SUPPLIER_KEEP_PROXY"):
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(k, None)
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    return gspread.authorize(creds), creds.service_account_email


def ensure_sheet(sh, title, rows, cols):
    """拿工作表；不存在就建；行数不够就扩容。"""
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=rows, cols=cols)
        log(f"新建工作表「{title}」")
    if ws.row_count < rows or ws.col_count < cols:
        ws.resize(rows=max(rows, ws.row_count), cols=max(cols, ws.col_count))
        log(f"「{title}」扩容到 {max(rows, ws.row_count)} 行 × {max(cols, ws.col_count)} 列")
    return ws


def build_summary(rows):
    """按品名聚合：去重手机号 = 供应商数（同一家多个规格只算一家）。"""
    by = collections.defaultdict(lambda: {"mob": set(), "n": 0, "date": "", "loc": collections.Counter()})
    for r in rows:
        name = r[0]
        g = by[name]
        g["n"] += 1
        if r[7]:
            g["mob"].add(r[7])
        if r[9] > g["date"]:
            g["date"] = r[9]
        if r[3]:
            g["loc"][r[3]] += 1
    out = []
    for name, g in by.items():
        loc = "、".join(k for k, _ in g["loc"].most_common(3))
        out.append([name, len(g["mob"]), g["n"], g["date"], loc])
    out.sort(key=lambda x: (-x[1], -x[2], x[0]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DATA_CSV)
    ap.add_argument("--dry-run", action="store_true", help="只统计不写表")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        raise SystemExit(f"没找到 {args.csv}，先跑 supplier_crawl.py")

    with open(args.csv, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    # 丢掉「更新时间」列（全表同一天，改放标题行）
    if header and header[0] == CSV_DATE_COL:
        header = header[1:]
        rows = [r[1:] for r in rows]
    log(f"读到 {len(rows)} 行 × {len(header)} 列")
    if header != HEADER:
        log(f"⚠️ CSV 表头与预期不一致，按 CSV 实际表头写入：{header}")
        header = header or HEADER

    summary = build_summary(rows)
    log(f"覆盖 {len(summary)} 个品名；供应商数 Top5："
        + "，".join(f"{s[0]}({s[1]})" for s in summary[:5]))

    if args.dry_run:
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

    day = time.strftime("%Y-%m-%d")
    ncol = len(header)
    title = f"药通网供货信息 · 数据日期 {day} · 共 {len(rows)} 条报价 / {len(summary)} 个品名"

    # ---------- 主表：全量覆盖 ----------
    ws = ensure_sheet(sh, MAIN_SHEET, len(rows) + 100, ncol)
    t0 = time.time()
    # 关键：clear 不解除合并，不先 unmerge 会导致 A 列只留左上角一个值
    sh.batch_update({"requests": [{"unmergeCells": {"range": {"sheetId": ws.id}}}]})
    ws.clear()
    log("已解除合并 + 清空旧数据，开始写入 …")
    ws.update([[title]], "A1", value_input_option="RAW")
    ws.update([header], "A2", value_input_option="RAW")
    written = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        ws.update(chunk, f"A{i + 3}", value_input_option="RAW")
        written += len(chunk)
        log(f"  写入 {written}/{len(rows)} 行")
    log(f"主表完成：{written} 行，用时 {time.time()-t0:.0f}s")

    # ---------- 汇总表 ----------
    ws2 = ensure_sheet(sh, SUM_SHEET, len(summary) + 50, len(SUM_HEADER))
    stitle = f"供应商数量汇总 · 数据日期 {day}"
    ws2.clear()
    ws2.update([[stitle]], "A1", value_input_option="RAW")
    ws2.update([SUM_HEADER] + summary, "A2", value_input_option="RAW")
    log(f"汇总表完成：{len(summary)} 行")

    # ---------- 格式（一次 batchUpdate 搞定，省配额） ----------
    hdr_fmt = {
        "backgroundColor": BROWN,
        "textFormat": {"bold": True, "foregroundColor": WHITE},
        "horizontalAlignment": "CENTER",
    }
    reqs = [
        # 解除合并后重新合并标题行
        {"mergeCells": {"range": {
            "sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": ncol}, "mergeType": "MERGE_ALL"}},
        {"mergeCells": {"range": {
            "sheetId": ws2.id, "startRowIndex": 0, "endRowIndex": 1,
            "startColumnIndex": 0, "endColumnIndex": len(SUM_HEADER)}, "mergeType": "MERGE_ALL"}},
        # 标题行样式
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.94, "green": 0.90, "blue": 0.84},
                "textFormat": {"bold": True, "fontSize": 11,
                               "foregroundColor": {"red": 0.36, "green": 0.24, "blue": 0.14}},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        {"repeatCell": {
            "range": {"sheetId": ws2.id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "backgroundColor": {"red": 0.94, "green": 0.90, "blue": 0.84},
                "textFormat": {"bold": True, "fontSize": 11,
                               "foregroundColor": {"red": 0.36, "green": 0.24, "blue": 0.14}},
                "horizontalAlignment": "LEFT", "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment,verticalAlignment)"}},
        # 表头样式（第 2 行）
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": hdr_fmt},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
        {"repeatCell": {
            "range": {"sheetId": ws2.id, "startRowIndex": 1, "endRowIndex": 2},
            "cell": {"userEnteredFormat": hdr_fmt},
            "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"}},
        # 冻结前两行（标题 + 表头）
        {"updateSheetProperties": {"properties": {
            "sheetId": ws.id, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        {"updateSheetProperties": {"properties": {
            "sheetId": ws2.id, "gridProperties": {"frozenRowCount": 2}},
            "fields": "gridProperties.frozenRowCount"}},
        # 筛选器：从表头行开始
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": ws.id, "startRowIndex": 1, "endRowIndex": len(rows) + 2,
            "startColumnIndex": 0, "endColumnIndex": ncol}}}},
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": ws2.id, "startRowIndex": 1, "endRowIndex": len(summary) + 2,
            "startColumnIndex": 0, "endColumnIndex": len(SUM_HEADER)}}}},
    ]
    # 列宽：供货说明、手机、货源所在地给足
    widths = {"供货说明": 300, "手机": 110, "货源所在地": 90, "品名": 90}
    for col, px in widths.items():
        if col in header:
            idx = header.index(col)
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": idx, "endIndex": idx + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    try:
        sh.batch_update({"requests": reqs})
        log("格式设置完成（标题 / 表头配色 / 冻结两行 / 筛选器 / 列宽）")
    except Exception as e:
        log(f"⚠️ 格式设置失败（数据已写入，不影响内容）：{type(e).__name__}: {e}")

    log(f"全部完成 → https://docs.google.com/spreadsheets/d/{sid}/edit")


if __name__ == "__main__":
    main()
