# -*- coding: utf-8 -*-
r"""
把康美香料价格推到 Google 表格（每天全量覆盖）

表格：sheet_id_spice.txt 里那一份（也可以给环境变量 SHEET_ID_SPICE）
    · 香料价格   —— 明细：品名/规格/产地 + 亳州 安国 成都 玉林 廉桥 普宁 六市场价
                    + 最高/最低/均价 + 周月季年涨跌 + 走势 + 更新时间（带时分）
                    涨跌榜末列、走势页顶部也带同一个更新时间
    · 香料涨跌榜 —— 同一批数据按「周涨跌」从大到小排，一眼看出谁在涨谁在跌
    · 香料走势   —— 每味一张折线图（近一年逐日价）+ 月度均价小表，对应药通网那种走势图

数据来源：km_spice.py（跑在阿里云函数上抓 kmzyw.com.cn，抓完提交 CSV/JSON 回仓库）
    km_spice_price.csv   明细
    km_spice_hist.json   走势

设计要点（沿用三条线已验证的规矩）
------------------------------------------------------------------
1. 全量覆盖：先 unmergeCells 再 clear 再写（clear 不解除合并，不 unmerge 会让 A 列只剩左上角）。
2. 普通表格：只做「冻结表头 + 表头加粗 + 筛选器 + 列宽」，**不加任何底色**。
   「涨/跌」靠文字（▲▼）表达，不靠颜色 —— 导出到 Excel 也不会丢信息。
3. RAW 写入：价格保持页面上给的两位小数，日期是 ISO 文本。
4. 图表数据放在隐藏表「走势数据」里，图表用 hiddenDimensionStrategy=SHOW_ALL 才能读到隐藏表。
5. 429 限速：整轮约 20 次请求，撞了就等 70 秒重试。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe km_spice_push.py            # 推表
    .venv\Scripts\python.exe km_spice_push.py --dry-run  # 只统计不写表
"""
import argparse
import csv
import gzip
import json
import os
import time

import gspread
from gspread.exceptions import APIError

from km_spice import MARKETS, PRICE_HEADER
from supplier_push import BATCH, connect, ensure_sheet, log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PRICE = os.path.join(BASE_DIR, "km_spice_price.csv")
JSON_HIST = os.path.join(BASE_DIR, "km_spice_hist.json")
SHEET_ID_FILE = os.path.join(BASE_DIR, "sheet_id_spice.txt")

TAB_PRICE = "香料价格"
TAB_RANK = "香料涨跌榜"
TAB_TREND = "香料走势"
TAB_POOL = "走势数据"

RANK_HEADER = ["品名", "规格", "产地", "现价", "周涨跌", "月涨跌", "季涨跌", "年涨跌", "走势"]
CHANGE_COLS = ["周涨跌", "月涨跌", "季涨跌", "年涨跌"]

# 明细表里的「历史价格」列：跟药通网一样放一个链接，点开就是这味的月价格曲线
LINK_COL = "历史价格"
LINK_TEXT = "点击查看"

# 走势页：每味一块。A:B 放月度均价小表，图贴在 C 列，块高 16 行
BLOCK_H = 16
CHART_W, CHART_H = 900, 280

WIDTHS = {"品名": 110, "规格": 120, "产地": 90, "更新时间": 145, "更新日期": 100,
          "历史价格": 105, "六市场均价": 110, "最高价": 90, "最低价": 90, "现价": 90}


def api(fn, *a, **kw):
    """429 限速保护：撞了就等 70 秒再试，最多 3 轮。"""
    for i in range(3):
        try:
            return fn(*a, **kw)
        except APIError as e:
            if "429" not in str(e) or i == 2:
                raise
            log("  ⚠️ 撞到 429 限速，等 70 秒后重试（第 %d 次）" % (i + 1))
            time.sleep(70)


def require_sheet_id():
    sid = os.environ.get("SHEET_ID_SPICE", "").strip()
    if not sid and os.path.exists(SHEET_ID_FILE):
        sid = open(SHEET_ID_FILE, encoding="utf-8").read().strip()
    if not sid:
        raise SystemExit(
            "找不到香料价格表 ID。\n"
            "  请把表格 ID 写进 %s（一行），或设环境变量 SHEET_ID_SPICE。" % SHEET_ID_FILE)
    return sid


def arrow(p):
    """涨跌 → 带箭头文字：+3.2% → ▲ 3.20%，-1.8% → ▼ 1.80%，0 → 持平"""
    if p is None or p == "":
        return ""
    try:
        v = float(p)
    except (TypeError, ValueError):
        return str(p)
    if v > 0:
        return "▲ %.2f%%" % v
    if v < 0:
        return "▼ %.2f%%" % abs(v)
    return "持平"


def load():
    if not os.path.exists(CSV_PRICE):
        raise SystemExit("没找到 %s，先跑 km_spice.py" % CSV_PRICE)
    with open(CSV_PRICE, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    # 走势：仓库里存的是 gz（阿里云函数提交，约 39KB），本机跑时是 .json
    hist = {}
    if os.path.exists(JSON_HIST + ".gz"):
        with gzip.open(JSON_HIST + ".gz", "rt", encoding="utf-8") as f:
            hist = json.load(f)
    elif os.path.exists(JSON_HIST):
        hist = json.load(open(JSON_HIST, encoding="utf-8"))
    return header, rows, hist


def monthly(series):
    """逐日 → 月度均价 [(YYYY-MM, 均价), ...]"""
    acc = {}
    for d, p in series:
        m = d[:7]
        a = acc.setdefault(m, [0.0, 0])
        a[0] += p
        a[1] += 1
    return [(m, round(acc[m][0] / acc[m][1], 2)) for m in sorted(acc)]


def months_of(rec):
    """一味的月价格 [(YYYY-MM, 价), ...]：优先用站点给的 monthly，没有就从逐日点算"""
    ms = rec.get("monthly") or []
    if ms:
        return [("%04d-%02d" % (m["year"], m["month"]), m["price"]) for m in ms]
    return monthly(rec.get("series") or [])


def stat_line(series):
    """标题后面那句：区间 / 最新 / 一年高 / 一年低 / 涨跌幅"""
    if not series:
        return ""
    prices = [p for _, p in series]
    hi, lo = max(prices), min(prices)
    last, first = series[-1][1], series[0][1]
    chg = (last - first) / first * 100 if first else 0
    return ("%s ~ %s ｜ 最新 %.2f ｜ 一年最高 %.2f ｜ 一年最低 %.2f ｜ 区间 %+.1f%%"
            % (series[0][0], series[-1][0], last, hi, lo, chg))


def sheet_or_none(sh, title):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return None


def write_table(sh, title, header, rows, widths=True):
    """一张普通表：unmerge → clear → RAW 覆盖写 → 冻结表头/加粗/筛选器/列宽（无底色）"""
    ws = ensure_sheet(sh, title, len(rows) + 50, len(header))
    api(sh.batch_update, {"requests": [{"unmergeCells": {"range": {"sheetId": ws.id}}}]})
    ws.clear()
    api(ws.update, [header], "A1", value_input_option="RAW")
    n = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        api(ws.update, chunk, "A%d" % (i + 2), value_input_option="RAW")
        n += len(chunk)
    reqs = [
        {"updateCells": {"range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}},
        {"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat"}},
        {"updateSheetProperties": {
            "properties": {"sheetId": ws.id, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount"}},
        {"setBasicFilter": {"filter": {"range": {
            "sheetId": ws.id, "startRowIndex": 0, "endRowIndex": len(rows) + 1,
            "startColumnIndex": 0, "endColumnIndex": len(header)}}}},
    ]
    if widths:
        for col, px in WIDTHS.items():
            if col in header:
                i = header.index(col)
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                              "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": px}, "fields": "pixelSize"}})
    api(sh.batch_update, {"requests": reqs})
    log("「%s」写入 %d 行 × %d 列" % (title, n, len(header)))
    return ws


def write_pool(sh, items):
    """隐藏表「走势数据」：每味一段 [月份, 月价格]，返回每段的 0-based 行范围

    用「月价格」而不是逐日点：一是和网页上月价格曲线一致，
    二是列少（35 vs 364），83 味合计 3 千行左右，建图更快、也不容易撞单元格上限。
    """
    blocks = [(key, rec) for key, rec in items]
    total = sum(1 + len(months_of(r)) for _, r in blocks) + 20
    ws = ensure_sheet(sh, TAB_POOL, total, 2)
    ws.clear()
    buf, spans = [], []
    for key, rec in blocks:
        start = len(buf)
        buf.append(["月份", "月价格(元/千克)"])
        for m, p in months_of(rec):
            buf.append([m, p])
        spans.append((key, start, len(buf)))       # endRowIndex 是开区间
    for i in range(0, len(buf), BATCH):
        api(ws.update, buf[i:i + BATCH], "A%d" % (i + 1), value_input_option="RAW")
    api(sh.batch_update, {"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}]})
    log("「%s」写入 %d 行月价格数据源（已隐藏）" % (TAB_POOL, len(buf)))
    return ws, spans


def chart_req(chart_id, title, pool_id, r0, r1):
    """一张月价格折线图，数据源是指定那一味的月价格段"""
    return {"addChart": {"chart": {
        "chartId": chart_id,
        "spec": {
            "title": title,
            # 数据源表是隐藏的，必须显式声明读隐藏行列，否则图出来是空的
            "hiddenDimensionStrategy": "SHOW_ALL",
            "basicChart": {
                "chartType": "LINE",
                "legendPosition": "NO_LEGEND",
                "axis": [{"position": "BOTTOM_AXIS", "title": "月份"},
                         {"position": "LEFT_AXIS", "title": "元/千克"}],
                "domains": [{"domain": {"sourceRange": {"sources": [{
                    "sheetId": pool_id, "startRowIndex": r0, "endRowIndex": r1,
                    "startColumnIndex": 0, "endColumnIndex": 1}]}}}],
                "series": [{"series": {"sourceRange": {"sources": [{
                    "sheetId": pool_id, "startRowIndex": r0, "endRowIndex": r1,
                    "startColumnIndex": 1, "endColumnIndex": 2}]}},
                    "targetAxis": "LEFT_AXIS"}],
                "headerCount": 1,
            },
        },
        # 图不贴在走势页上（那页放不下 80+ 张），只作为「历史价格」列的跳转目标
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": pool_id, "rowIndex": 0, "columnIndex": 0},
            "widthPixels": CHART_W, "heightPixels": CHART_H}},
    }}}


CHART_SHEET = "香料价格图"


def write_trend(sh, items, stamp):
    """走势页：顶部一行数据更新时间，之后每味一个区块（标题 + 月价格表 + 折线图）

    这一页同时充当「历史价格」的超链接目标：#gid=<本页>&range=C{行}:C{行}
    所以区块行号必须和「香料价格」每一行的顺序严格对应。
    """
    ws = ensure_sheet(sh, TAB_TREND, len(items) * BLOCK_H + 40, 12)
    api(sh.batch_update, {"requests": [{"updateCells": {
        "range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}}]})
    ws.clear()

    pool, spans = write_pool(sh, items)
    span_map = {k: (r0, r1) for k, r0, r1 in spans}

    area = [["数据更新时间：%s（北京时间） ｜ 数据来源：康美中药网 kmzyw.com.cn ｜ 单位：元/千克"
             % stamp] + [""] * 11]
    reqs, anchors = [], []
    for idx, (key, rec) in enumerate(items):
        name = key.replace("|", " ")
        bs = idx * BLOCK_H + 1
        while len(area) < bs + BLOCK_H:
            area.append([""] * 12)
        area[bs][0] = "%s — 月价格走势" % name
        area[bs][2] = stat_line(rec.get("series") or [])
        area[bs + 1][0], area[bs + 1][1] = "月份", "月均价"
        for j, (m, v) in enumerate(months_of(rec), start=2):
            if bs + j < len(area):
                area[bs + j][0], area[bs + j][1] = m, v
        r0, r1 = span_map[key]
        reqs.append(chart_req(idx + 1, name, pool.id, r0, r1))
        # 跳转落点：区块标题那一格（1-based 行号 = bs + 1）
        anchors.append(col_letter(3) + str(bs + 1))     # C 列

    api(ws.update, area, "A1", value_input_option="RAW")

    # 旧图清理：先查出已有的图表并删掉（每天全量重建）
    meta = api(sh.fetch_sheet_metadata, {"fields": "sheets(charts(chartId))"})
    old = [ch["chartId"] for s in meta.get("sheets", [])
           for ch in (s.get("charts") or [])]
    if old:
        api(sh.batch_update, {"requests": [{"deleteEmbeddedObject": {"objectId": cid}}
                                           for cid in old]})
        log("  清掉旧图 %d 张" % len(old))
    for i in range(0, len(reqs), 100):
        api(sh.batch_update, {"requests": reqs[i:i + 100]})
    log("「%s」%d 味月价格图 + 月度均价表" % (TAB_TREND, len(reqs)))
    return ws, anchors


def col_letter(n):
    """1 → A，3 → C，27 → AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    header, rows, hist = load()
    idx = {c: i for i, c in enumerate(header)}

    def cell(r, c):
        i = idx.get(c)
        return r[i] if i is not None and i < len(r) else ""

    # 抓取时刻：新 CSV 带「更新时间」（带时分）；兼容旧的「更新日期」只有日期
    stamp = (cell(rows[0], "更新时间") if rows else "") or \
            (cell(rows[0], "更新日期") if rows else "") or \
            time.strftime("%Y-%m-%d %H:%M")

    # 涨跌榜：按周涨跌从大到小，没有涨跌数据的排最后
    def wk(r):
        try:
            return float(cell(r, "周涨跌") or "")
        except ValueError:
            return -999
    rank = sorted(rows, key=lambda r: (-wk(r), cell(r, "品名")))
    rank_rows = []
    for r in rank:
        line = [cell(r, c) for c in ("品名", "规格", "产地", "现价")]
        line += [arrow(cell(r, c)) for c in CHANGE_COLS]
        line.append(cell(r, "走势"))
        rank_rows.append(line)

    # 走势/月价格：按「品名|规格|产地」排序，明细表也按同一顺序写，
    # 这样第 n 味 ↔ 走势页第 n 块，超链接才对得上。
    def key_of(r):
        return "%s|%s|%s" % (cell(r, "品名"), cell(r, "规格"), cell(r, "产地"))

    items = sorted(((key_of(r), hist[key_of(r)]) for r in rows if key_of(r) in hist),
                   key=lambda x: x[0])

    log("明细 %d 行 × %d 列；涨跌榜 %d 行；走势 %d 味"
        % (len(rows), len(header), len(rank_rows), len(items)))
    if rows:
        log("样例：%s / %s / %s  周 %s"
            % (cell(rows[0], "品名"), cell(rows[0], "规格"),
               cell(rows[0], "产地"), arrow(cell(rows[0], "周涨跌"))))
    up = sum(1 for r in rank_rows if r[4].startswith("▲"))
    down = sum(1 for r in rank_rows if r[4].startswith("▼"))
    log("周涨跌：涨 %d 味 / 跌 %d 味 / 其余持平或无数据" % (up, down))

    if a.dry_run:
        log("--dry-run，不写表")
        return

    gc, sa_email = connect()
    sid = require_sheet_id()
    try:
        sh = gc.open_by_key(sid)
    except (gspread.SpreadsheetNotFound, PermissionError) as e:
        # 403 是「没共享给服务账号」，404 是 ID 不对或没共享，两者对人来说要做的是同一件事
        raise SystemExit(
            "服务账号 %s 打不开这个表格（%s）\n"
            "→ 在 Google 表格点右上角「共享」，把这个邮箱加成「编辑者」，再重跑一次。"
            % (sa_email, type(e).__name__))
    log("打开表格「%s」" % sh.title)

    # 删掉新建表格自带的空白 Sheet1（只删确实是空的那种，有数据绝不动）
    for name in ("Sheet1", "工作表1", "Sheet"):
        ws0 = sheet_or_none(sh, name)
        if ws0 is None:
            continue
        try:
            if not ws0.get_all_values():
                api(sh.del_worksheet, ws0)
                log("删掉空白默认工作表「%s」" % name)
        except Exception as e:
            log("跳过删除「%s」：%s" % (name, e))

    trend_ws, anchors = write_trend(sh, items, stamp)

    # 「历史价格」列：放在「更新时间」前一列，每行一个可点的超链接，跳到走势页对应那味
    # 明细行顺序 = items 顺序（都按 品名|规格|产地 排），所以第 i 行对第 i 块
    links = None
    if LINK_COL in header:
        col = header.index(LINK_COL)
        links = []
        for i in range(len(rows)):
            anchor = anchors[i] if i < len(anchors) else "C1"
            url = ("https://docs.google.com/spreadsheets/d/%s/edit#gid=%d&range=%s"
                   % (sid, trend_ws.id, anchor))
            links.append(['=HYPERLINK("%s","%s")' % (url, LINK_TEXT)])

    ws_price = write_table(sh, TAB_PRICE, header, rows)
    if links:
        api(ws_price.update, links, "%s2" % col_letter(col + 1),
            value_input_option="USER_ENTERED")
        log("「%s」写入 %d 个历史价格跳转链接（末列之前）" % (TAB_PRICE, len(links)))
    write_table(sh, TAB_RANK, RANK_HEADER, rank_rows)

    log("全部完成 → https://docs.google.com/spreadsheets/d/%s/edit" % sid)


if __name__ == "__main__":
    main()
