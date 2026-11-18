# -*- coding: utf-8 -*-
r"""
把中香网「香料行情」价格推到 Google 表格（每天全量覆盖）

表格：sheet_id_zxprice.txt（或环境变量 SHEET_ID_ZXPRICE）
    · 香料价格   —— 第 1 页。135 行明细：市场/品类/产地/规格 + 今日价 前日价 日涨跌
                    + 较前日 较月初 较年初 + 单位 + 历史价格(跳转) + 更新时间
    · 香料走势1…N —— 每行一块：日价格折线图 + 「价格节点」小表（年初/月初/前日/今日）
                    135 块 → 2 页，每页 80 块
    · 走势数据    —— 隐藏，图的取数区

为什么是「日价格」而不是康美那种「月均价」
------------------------------------------------------------------
中香网不开放历史价格接口（见 zx_price.py 顶部实测），历史只能每天累积（zx_price_hist.json.gz）。
所以曲线从 2026-11-18 开始一天一个点，越跑越长。
为了让第一天的图就有内容，另外补两个锚点：
    年初价 = 今日价 / (1 + 较年初)      官方涨跌幅是指标，反推是精确的
    月初价 = 今日价 / (1 + 较月初)
这两个点在小表里明确标成「反推」，不写进历史文件，只用于画图。

设计要点（与康美线同一套，已验证）
------------------------------------------------------------------
1. 全量覆盖：unmerge → clear → 写；不加底色；冻结表头 + 表头加粗 + 筛选 + 列宽。
2. 图表数据放在隐藏表，图表要 hiddenDimensionStrategy=SHOW_ALL 才读得到。
3. 图例参数是 TOP_LEGEND（写 TOP 会 400）。
4. 429：全局限速 + 撞了就等。
5. 不能把可见工作表删光 —— 先建新页，最后删旧页。

用法
------------------------------------------------------------------
    .venv\Scripts\python.exe zx_price_push.py --dry-run
    .venv\Scripts\python.exe zx_price_push.py
"""
import argparse
import csv
import gzip
import json
import os
import time

import gspread
from gspread.exceptions import APIError

from supplier_push import BATCH, connect, ensure_sheet, log
from zx_price import HEADER

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PRICE = os.path.join(BASE_DIR, "zx_price.csv")
JSON_HIST = os.path.join(BASE_DIR, "zx_price_hist.json")
SHEET_ID_FILE = os.path.join(BASE_DIR, "sheet_id_zxprice.txt")

TAB_PRICE = "香料价格"
TAB_TREND = "香料走势"          # 实际叫 香料走势1、香料走势2 …
TAB_POOL = "走势数据"

LINK_COL = "历史价格"
LINK_TEXT = "点击查看"

PER_PAGE = 80               # 一页放多少块（一块 = 一行报价）
CHART_OFF = 1
SUM_TITLE_OFF = 17
SUM_HEAD_OFF = 18
SUM_DATA_OFF = 19
SUM_ROWS = 4                # 年初 / 月初 / 前日 / 今日
BLOCK_H = SUM_DATA_OFF + SUM_ROWS + 1
CHART_W, CHART_H = 980, 300
COLS_TREND = 4

WIDTHS = {"市场": 70, "品类": 120, "产地": 110, "规格": 150, "今日价": 90,
          "前日价": 90, "日涨跌": 85, "较前日": 95, "较月初": 95, "较年初": 95,
          "单位": 85, "历史价格": 105, "更新时间": 145}


def api(fn, *a, **kw):
    for i in range(4):
        try:
            return fn(*a, **kw)
        except APIError as e:
            if "429" not in str(e) or i == 3:
                raise
            log("  ⚠️ 撞到 429 限速，等 65 秒后重试（第 %d 次）" % (i + 1))
            time.sleep(65)


MIN_GAP = 1.15
_LAST = [0.0]


def throttle(gc):
    orig = gc.http_client.request

    def wrapper(*a, **kw):
        gap = MIN_GAP - (time.time() - _LAST[0])
        if gap > 0:
            time.sleep(gap)
        try:
            return orig(*a, **kw)
        finally:
            _LAST[0] = time.time()

    gc.http_client.request = wrapper
    return gc


def require_sheet_id():
    sid = os.environ.get("SHEET_ID_ZXPRICE", "").strip()
    if not sid and os.path.exists(SHEET_ID_FILE):
        sid = open(SHEET_ID_FILE, encoding="utf-8").read().strip()
    if not sid:
        raise SystemExit(
            "找不到中香网价格表 ID。\n"
            "  把表格 ID 写进 %s（一行），或设环境变量 SHEET_ID_ZXPRICE。" % SHEET_ID_FILE)
    return sid


def load():
    if not os.path.exists(CSV_PRICE):
        raise SystemExit("没找到 %s，先跑 zx_price.py" % CSV_PRICE)
    with open(CSV_PRICE, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    hist = {}
    if os.path.exists(JSON_HIST + ".gz"):
        with gzip.open(JSON_HIST + ".gz", "rt", encoding="utf-8") as f:
            hist = json.load(f)
    elif os.path.exists(JSON_HIST):
        with open(JSON_HIST, encoding="utf-8") as f:
            hist = json.load(f)
    return header, rows, hist


def pct(s):
    """'-12.12%' → -0.1212"""
    s = (s or "").strip().replace("%", "")
    if not s:
        return None
    try:
        return float(s) / 100.0
    except ValueError:
        return None


def num(s, nd=None):
    s = (s or "").strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return round(v, nd) if nd is not None else v


def key_of(name, producer, spec, market):
    return "%s|%s|%s|%s" % (market, name, producer, spec)


def series_of(key, rec, hist):
    """一条报价的逐日价格：历史累积点 + 月初/年初两个反推锚点"""
    qdate = rec["date"]
    pts = {d: v for d, v in (hist.get(key) or {}).items() if v}
    for field, day in (("yoy", qdate[:4] + "-01-01"), ("mom", qdate[:7] + "-01")):
        p = rec.get(field)
        if p is None or p <= -0.999 or day in pts or rec["today"] is None:
            continue
        pts[day] = round(rec["today"] / (1.0 + p), 2)
    return [(d, round(float(v), 2)) for d, v in sorted(pts.items())]


def sheet_or_none(sh, title):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return None


def col_letter(n):
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_table(sh, title, header, rows):
    ws = ensure_sheet(sh, title, len(rows) + 50, len(header))
    api(sh.batch_update, {"requests": [{"unmergeCells": {"range": {"sheetId": ws.id}}}]})
    api(ws.clear)
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
    """隐藏表：每条报价一段 [日期, 价格]，返回每段 0-based 行范围"""
    total = sum(1 + len(pts) for _, pts, _, _ in items) + 20
    ws = ensure_sheet(sh, TAB_POOL, total, 2)
    api(ws.clear)
    buf, spans = [], []
    for key, pts, label, _ in items:
        start = len(buf)
        buf.append(["日期", label])
        for d, p in pts:
            buf.append([d, p])
        spans.append((key, start, len(buf)))
    for i in range(0, len(buf), BATCH):
        api(ws.update, buf[i:i + BATCH], "A%d" % (i + 1), value_input_option="RAW")
    api(sh.batch_update, {"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}]})
    log("「%s」写入 %d 行价格数据源（已隐藏）" % (TAB_POOL, len(buf)))
    return ws, spans


def chart_req(pool_id, trend_id, r0, r1, anchor_row, unit):
    return {"addChart": {"chart": {
        "spec": {
            "title": "",
            "hiddenDimensionStrategy": "SHOW_ALL",
            "basicChart": {
                "chartType": "LINE",
                "legendPosition": "TOP_LEGEND",
                "axis": [{"position": "BOTTOM_AXIS", "title": "日期"},
                         {"position": "LEFT_AXIS", "title": unit or "价格"}],
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
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": trend_id, "rowIndex": anchor_row, "columnIndex": 0},
            "widthPixels": CHART_W, "heightPixels": CHART_H}},
    }}}


def write_trend_pages(sh, items, stamp):
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)

    meta = api(sh.fetch_sheet_metadata,
               {"fields": "sheets(properties(sheetId,title),charts(chartId))"})
    old = [ch["chartId"] for s in meta.get("sheets", [])
           for ch in (s.get("charts") or [])]
    if old:
        api(sh.batch_update, {"requests": [{"deleteEmbeddedObject": {"objectId": c}}
                                           for c in old]})
        log("  清掉旧图 %d 张" % len(old))

    pool, spans = write_pool(sh, items)
    span_map = {k: (a, b) for k, a, b in spans}

    anchors, charts = {}, 0
    for p in range(pages):
        title = "%s%d" % (TAB_TREND, p + 1)
        chunk = items[p * PER_PAGE:(p + 1) * PER_PAGE]
        total = 1 + len(chunk) * BLOCK_H + 4
        ws = ensure_sheet(sh, title, total, COLS_TREND)
        api(sh.batch_update, {"requests": [{"updateCells": {
            "range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}}]})
        api(ws.clear)

        area = [[""] * COLS_TREND for _ in range(total)]
        area[0][0] = ("数据更新时间：%s ｜ 数据来源：中香网 zhongxiangwang.cn ｜ "
                      "价格为「品类 + 产地 + 规格」行情价 ｜ 第 %d/%d 页"
                      % (stamp, p + 1, pages))

        reqs, bold = [], []
        for idx, (key, pts, label, rec) in enumerate(chunk):
            bs = 1 + idx * BLOCK_H
            pmap = dict(pts)

            area[bs][0] = "日价格走势图：%s" % label
            if pts:
                area[bs][3] = "最新价：%.2f %s" % (pts[-1][1], rec["unit"] or "")
            bold.append(bs)

            area[bs + SUM_TITLE_OFF][0] = "价格节点：%s" % label
            bold.append(bs + SUM_TITLE_OFF)

            head = bs + SUM_HEAD_OFF
            for i, t in enumerate(["节点", "日期", "价格", "说明"]):
                area[head][i] = t
            bold.append(head)

            nodes = [
                ("年初", rec["date"][:4] + "-01-01",
                 "按官方「较年初 %+.2f%%」反推" % (rec["yoy"] * 100) if rec["yoy"] is not None
                 else "官方未给"),
                ("月初", rec["date"][:7] + "-01",
                 "按官方「较月初 %+.2f%%」反推" % (rec["mom"] * 100) if rec["mom"] is not None
                 else "官方未给"),
                ("前日", rec["prev_date"] or "—", "官方前日价"),
                ("今日", rec["date"], "官方今日价"),
            ]
            for j, (nm, day, desc) in enumerate(nodes):
                r = bs + SUM_DATA_OFF + j
                v = pmap.get(day)
                area[r][0] = nm
                area[r][1] = day
                area[r][2] = "—" if v is None else round(float(v), 2)
                area[r][3] = desc

            r0, r1 = span_map[key]
            if len(pts) >= 2:
                reqs.append(chart_req(pool.id, ws.id, r0, r1, bs + CHART_OFF, rec["unit"]))
                charts += 1
            anchors[key] = (ws.id, "A%d" % (bs + 1))

        api(ws.update, area, "A1", value_input_option="RAW")

        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": total,
                      "startColumnIndex": 2, "endColumnIndex": 3},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER",
                                                           "pattern": "0.00"}}},
            "fields": "userEnteredFormat.numberFormat"}})
        for start, px in ((0, 300), (1, 105), (2, 80), (3, 300)):
            reqs.append({"updateDimensionProperties": {
                "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                          "startIndex": start, "endIndex": start + 1},
                "properties": {"pixelSize": px}, "fields": "pixelSize"}})
        for bs in bold:
            reqs.append({"repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": bs, "endRowIndex": bs + 1,
                          "startColumnIndex": 0, "endColumnIndex": COLS_TREND},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                "fields": "userEnteredFormat.textFormat"}})

        chart_reqs = [r for r in reqs if "addChart" in r]
        fmt_reqs = [r for r in reqs if "addChart" not in r]
        for i in range(0, len(fmt_reqs), 100):
            api(sh.batch_update, {"requests": fmt_reqs[i:i + 100]})
        for i in range(0, len(chart_reqs), 40):
            api(sh.batch_update, {"requests": chart_reqs[i:i + 40]})
        log("「%s」%d 块（图 %d 张）" % (title, len(chunk), len(chart_reqs)))

    log("「%s」共 %d 页 / %d 块 / %d 张图" % (TAB_TREND, pages, len(items), charts))
    return anchors


def reorder(sh, order):
    meta = api(sh.fetch_sheet_metadata, {"fields": "sheets(properties(sheetId,title))"})
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    reqs = [{"updateSheetProperties": {"properties": {"sheetId": ids[n], "index": i},
                                       "fields": "index"}}
            for i, n in enumerate(order) if n in ids]
    if reqs:
        api(sh.batch_update, {"requests": reqs})
        log("工作表顺序 → 香料价格 / 香料走势1…%d / 走势数据" % (len(order) - 2))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    header, rows, hist = load()
    idx = {c: i for i, c in enumerate(header)}

    def cell(r, c):
        i = idx.get(c)
        return r[i] if i is not None and i < len(r) else ""

    if not rows:
        raise SystemExit("CSV 是空的，先跑 zx_price.py")

    stamp = cell(rows[0], "更新时间") or time.strftime("%Y-%m-%d %H:%M")
    qdate = stamp[:10]

    # 价格表表头 = CSV 表头，在「更新时间」前插一列「历史价格」
    out_header = list(header)
    if LINK_COL not in out_header:
        out_header.insert(out_header.index("更新时间"), LINK_COL)
    insert_at = out_header.index(LINK_COL)

    items = []
    for r in rows:
        name, producer, spec, market = (cell(r, "品类"), cell(r, "产地"),
                                        cell(r, "规格"), cell(r, "市场"))
        key = key_of(name, producer, spec, market)
        ser = hist.get(key) or {}
        prev_days = [d for d in ser if d < qdate]
        rec = {
            "date": qdate,
            "unit": cell(r, "单位"),
            "today": num(cell(r, "今日价")),
            "prev": num(cell(r, "前日价")),
            "prev_date": max(prev_days) if prev_days else "",
            "dod": pct(cell(r, "较前日")),
            "mom": pct(cell(r, "较月初")),
            "yoy": pct(cell(r, "较年初")),
        }
        label = "%s · %s · %s（%s）" % (name, producer, spec, market)
        items.append((key, series_of(key, rec, hist), label, rec))

    up = sum(1 for r in rows if (num(cell(r, "日涨跌")) or 0) > 0)
    down = sum(1 for r in rows if (num(cell(r, "日涨跌")) or 0) < 0)
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    log("明细 %d 行 × %d 列；走势 %d 块 → %d 页（每页 %d 块）；日涨跌 涨 %d / 跌 %d / 平 %d"
        % (len(rows), len(out_header), len(items), pages, PER_PAGE,
           up, down, len(rows) - up - down))
    if items:
        log("样例：%s → %d 个价格点" % (items[0][2], len(items[0][1])))

    if a.dry_run:
        log("--dry-run，不写表")
        return

    gc, sa_email = connect()
    gc = throttle(gc)
    sid = require_sheet_id()
    try:
        sh = gc.open_by_key(sid)
    except (gspread.SpreadsheetNotFound, PermissionError):
        raise SystemExit(
            "服务账号 %s 打不开这个表格\n"
            "→ 在 Google 表格点右上角「共享」，把这个邮箱加成「编辑者」，再重跑一次。"
            % sa_email)
    log("打开表格「%s」" % sh.title)

    visible = [s["properties"]["title"] for s in
               api(sh.fetch_sheet_metadata, {"fields": "sheets(properties(title,hidden))"}
                   ).get("sheets", []) if not s["properties"].get("hidden")]
    for name in ("Sheet1", "工作表1", "Sheet"):
        ws0 = sheet_or_none(sh, name)
        if ws0 is None or len(visible) <= 1:
            continue
        try:
            if not ws0.get_all_values():
                api(sh.del_worksheet, ws0)
                if name in visible:
                    visible.remove(name)
                log("删掉空白默认工作表「%s」" % name)
        except Exception as e:
            log("跳过删除「%s」：%s" % (name, e))

    anchors = write_trend_pages(sh, items, stamp)

    # 「历史价格」列：每行一个可点的超链接，跳到走势页对应那一块
    links = []
    for r in rows:
        key = key_of(cell(r, "品类"), cell(r, "产地"), cell(r, "规格"), cell(r, "市场"))
        gid, anchor = anchors.get(key, ("", "A1"))
        url = ("https://docs.google.com/spreadsheets/d/%s/edit#gid=%s&range=%s"
               % (sid, gid, anchor))
        links.append(['=HYPERLINK("%s","%s")' % (url, LINK_TEXT)])

    filled = [list(r)[:insert_at] + [""] + list(r)[insert_at:] for r in rows]
    filled = [(rr + [""] * len(out_header))[:len(out_header)] for rr in filled]

    ws_price = write_table(sh, TAB_PRICE, out_header, filled)
    api(ws_price.update, links, "%s2" % col_letter(insert_at + 1),
        value_input_option="USER_ENTERED")
    log("「%s」写入 %d 个历史价格跳转链接" % (TAB_PRICE, len(links)))

    reorder(sh, [TAB_PRICE] + ["%s%d" % (TAB_TREND, p + 1) for p in range(pages)] + [TAB_POOL])
    log("全部完成 → https://docs.google.com/spreadsheets/d/%s/edit" % sid)


if __name__ == "__main__":
    main()
