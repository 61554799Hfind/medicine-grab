# -*- coding: utf-8 -*-
r"""
把三个站的求购数据推到 Google 表格（三个工作表，每天全量覆盖）

表格：sheet_id_purchase.txt 里那一份（老板新建的「求购信息」表）
    · 工作表「药通网」      —— 个人求购，按发布日期从早到晚
    · 工作表「康美中药城」  —— 需求信息，没有发布时间，按剩余天数从多到少
    · 工作表「中香网」      —— 求购，按发布日期从早到晚

设计要点（和供应商那条线同一套规矩）
------------------------------------------------------------------
1. 每天全量覆盖：先 clear() 再写，条数变少也不会留旧行。
2. 先 unmergeCells 整表：clear() 不解除合并，不先解除会让 A 列只剩左上角。
3. 普通表格：只做「冻结表头 + 表头加粗 + 筛选器 + 列宽」，不加任何底色。
4. RAW 写入：手机号、日期都保持文本，不会被转数字/科学计数。
5. 每批请求之间留间隔：写请求限额 60 次/分钟/用户。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe purchase_push.py             # 写表
    .venv\Scripts\python.exe purchase_push.py --dry-run   # 只看统计不写表
"""
import argparse
import csv
import os
import time

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SA_FILE = os.path.join(BASE_DIR, "service_account.json")
SHEET_ID_FILE = os.path.join(BASE_DIR, "sheet_id_purchase.txt")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
BATCH = 5000
GAP = 0.8          # 每次写请求之间的间隔，防 429

# (工作表名, CSV 文件)
TABS = [
    ("药通网", "purchase_ytw.csv"),
    ("康美中药城", "purchase_km.csv"),
    ("中香网", "purchase_zx.csv"),
]

WIDTHS = {
    "品名": 90, "规格": 110, "产地": 80, "货源所在地": 90, "单价": 80, "数量": 80,
    "联系人": 90, "手机": 115, "备用电话": 115, "电话": 115, "联系电话": 115,
    "求购说明": 240, "地址": 260, "发布日期": 95, "剩余天数": 80, "实名认证": 80,
    "有效期": 95, "截止日期": 95, "求购量": 90, "合作类型": 110,
    "仓储所在地": 150, "质量要求": 220, "包装要求": 110, "资质要求": 110,
    "认证信息": 80, "会员类型": 90, "经营所在地": 200, "详情链接": 300,
    "更新日期": 95,
}


def log(msg=""):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def require_sheet_id():
    sid = os.environ.get("SHEET_ID_PURCHASE", "").strip()
    if not sid and os.path.exists(SHEET_ID_FILE):
        sid = open(SHEET_ID_FILE, encoding="utf-8").read().strip()
    if not sid or "PASTE" in sid.upper():
        raise SystemExit(
            "找不到求购表格 ID。\n"
            f"  请把表格 ID 写进 {SHEET_ID_FILE}（一行），或设环境变量 SHEET_ID_PURCHASE。\n"
            "  ID 就是表格地址 /spreadsheets/d/<这串>/edit 中间那一串。")
    if len(sid) < 40:
        raise SystemExit(
            f"表格 ID 长度不对（{len(sid)} 位，正常 44 位）：{sid}\n"
            "  多半是从截图里手抄漏了字符，重新复制一遍地址栏。")
    return sid


def read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    return header, rows


def ensure_sheet(sh, title, rows, cols):
    """优先复用表格里已有的同名/近名工作表，避免在老板的表里多出一堆重复 tab。

    匹配规则：标题完全一致 > 标题包含关键字（药通 / 康美 / 中香）。
    复用时会沿用原有标题，不擅自改名。
    """
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        pass
    kw = title.replace("中药城", "").replace("网", "")
    for ws in sh.worksheets():
        t = ws.title
        if kw and kw in t:
            log(f"  复用已有工作表「{t}」（对应 {title}）")
            return ws
    ws = sh.add_worksheet(title=title, rows=max(rows, 100), cols=max(cols, 10))
    log(f"  新建工作表「{title}」")
    return ws


def write_tab(sh, title, header, rows):
    ncol = len(header)
    ws = ensure_sheet(sh, title, len(rows) + 100, ncol)
    if ws.row_count < len(rows) + 100 or ws.col_count < ncol:
        ws.resize(rows=max(len(rows) + 100, ws.row_count), cols=max(ncol, ws.col_count))
        time.sleep(GAP)
    # clear() 不解除合并 → 必须先 unmerge 整表，否则 A 列只留左上角
    try:
        sh.batch_update({"requests": [{"unmergeCells": {"range": {"sheetId": ws.id}}}]})
    except Exception:
        pass
    time.sleep(GAP)
    ws.clear()
    time.sleep(GAP)
    ws.update([header], "A1", value_input_option="RAW")
    time.sleep(GAP)
    written = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        ws.update(chunk, f"A{i + 2}", value_input_option="RAW")
        written += len(chunk)
        time.sleep(GAP)
    log(f"  「{title}」写入 {written} 行 × {ncol} 列")

    bold = {"textFormat": {"bold": True}}
    reqs = [
        # 先清掉所有自定义格式（老板要求普通表格、不要底色）
        {"updateCells": {"range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}},
        {"repeatCell": {"range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": bold},
                        "fields": "userEnteredFormat.textFormat"}},
        {"updateSheetProperties": {"properties": {
            "sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": ws.id, "startRowIndex": 0, "endRowIndex": len(rows) + 1,
            "startColumnIndex": 0, "endColumnIndex": ncol}}}},
    ]
    for col, px in WIDTHS.items():
        if col in header:
            idx = header.index(col)
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": idx, "endIndex": idx + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    try:
        sh.batch_update({"requests": reqs})
    except Exception as e:
        log(f"  ⚠️ 「{title}」格式设置失败（数据已写入）：{type(e).__name__}: {e}")
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = []
    for title, fn in TABS:
        header, rows = read_csv(os.path.join(BASE_DIR, fn))
        if not header:
            log(f"⚠️ 缺 {fn}，跳过「{title}」")
            continue
        payload.append((title, header, rows))
        log(f"读 {fn}: {len(rows)} 行 × {len(header)} 列")

    if not payload:
        raise SystemExit("没有可推送的数据，先跑 purchase_crawl.py")

    if args.dry_run:
        log("--dry-run，不写表")
        return

    sid = require_sheet_id()
    # 上传 Google 时必须走系统代理（国内直连不通），这里不清代理
    creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    try:
        sh = gc.open_by_key(sid)
    except gspread.SpreadsheetNotFound:
        raise SystemExit(
            f"服务账号 {creds.service_account_email} 打不开表格 {sid}\n"
            "→ 请在 Google 表格里点右上「共享」，把这个邮箱加成「编辑者」。")
    log(f"打开表格「{sh.title}」")

    for title, header, rows in payload:
        write_tab(sh, title, header, rows)

    log(f"全部完成 → https://docs.google.com/spreadsheets/d/{sid}/edit")


if __name__ == "__main__":
    main()
