# -*- coding: utf-8 -*-
r"""
把中香网「香料行情」价格推到 Google 表格（每天全量覆盖）

表格：sheet_id_zxprice.txt（或环境变量 SHEET_ID_ZXPRICE）

版式（2026-11-18 晚，按用户要求：国内/国外拆两张表；走势页做成康美那种「图 + 年×月矩阵」）

    国内香料价格   —— 120 行：品类/清单名/产地/规格 + 今日价 前日价 日涨跌 较前日 较月初 较年初
                      + 单位 + 历史价格(跳转) + 更新时间
    国外香料价格   —— 15 行，字段同上（清单名 = 该品类在 medicine_list 971 味里对应的标准名，
                      口径同康美线；清单外品类清单名留空）
    国内香料走势1…N —— 每行一块：月价格折线图 + 「历史月均价」年×月矩阵
    国外香料走势1…M
    走势数据       —— 隐藏，图的取数区（两个市场共用）

均价怎么来的（中香网没有历史接口，只能自己攒）
------------------------------------------------------------------
· 每天抓一次当日价，写进 zx_price_hist.json.gz（键 = 市场|品类|产地|规格）。
· 月均价 = 该月所有日价的算术平均。所以曲线从 2026-11 开始一月一个点，越跑越长。
· 为了第一个月图上不是孤零零一个点，补两个**反推锚点**（官方涨跌幅是指标，反推是精确的）：
      年初价 = 今日价 / (1 + 较年初)  →  放在当年 1 月
      月初价 = 今日价 / (1 + 较月初)  →  放在当月（当月已有真实日均价时不覆盖）
  这两个点只在缺月份时补，并在页首说明里注明「反推」。

设计要点（与康美线同一套，已验证）
------------------------------------------------------------------
1. 全量覆盖：unmerge → clear → 写；不加底色；冻结表头 + 表头加粗 + 筛选 + 列宽。
2. 图表数据放在隐藏表，图表要 hiddenDimensionStrategy=SHOW_ALL 才读得到。
3. 图例参数是 TOP_LEGEND（写 TOP 会 400）。
4. 429：全局限速 + 撞了就等。
5. 不能把可见工作表删光 —— 先建新页，最后删旧页。
6. 清旧图只做一次（两个市场共用一张隐藏数据页，不能各清一次，否则后一个市场会把前一个的图删掉）。

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

MARKETS = [("国内", "国内香料价格", "国内香料走势"),
           ("国外", "国外香料价格", "国外香料走势")]
TAB_POOL = "走势数据"

# 旧版页名（单表「香料价格/香料走势」时代），跑的时候顺手删掉
OLD_TABS_EXACT = ["香料价格"]
OLD_TABS_PREFIX = ["香料走势", "国内香料走势", "国外香料走势"]

LINK_COL = "历史价格"
LINK_TEXT = "点击查看"

MONTH_NAMES = ["一月", "二月", "三月", "四月", "五月", "六月",
               "七月", "八月", "九月", "十月", "十一月", "十二月"]

# 分卷：一页放多少块
PER_PAGE = 80

# 走势页每块排版（相对块首行的偏移，0-based）—— 与康美线一致
CHART_OFF = 1          # 图贴在第 2 行
TABLE_TITLE_OFF = 18   # 「历史月均价：…」
TABLE_HEAD_OFF = 19    # 年/月 + 一月…十二月
DATA_OFF = 20          # 数据从这一行开始
CHART_W, CHART_H = 980, 300
MAX_YEARS = 8

COLS_TREND = 13        # A 列标题 + 十二个月

WIDTHS = {"品类": 120, "清单名": 120, "产地": 110, "规格": 150, "今日价": 90,
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


def months_of(key, rec, hist):
    """一条报价的逐月均价：日价按月算术平均 + 缺月份用官方涨跌幅反推的锚点

    返回 [("2026-01", 22.25), ("2026-11", 23.10), ...]
    """
    agg = {}
    for d, v in (hist.get(key) or {}).items():
        if not v or len(d) < 7:
            continue
        agg.setdefault(d[:7], []).append(float(v))
    months = {ym: round(sum(vs) / len(vs), 2) for ym, vs in agg.items() if vs}

    today = rec.get("today")
    y, m = rec["date"][:4], rec["date"][5:7]
    if today is not None:
        for field, ym in (("yoy", "%s-01" % y), ("mom", "%s-%s" % (y, m))):
            p = rec.get(field)
            if p is None or p <= -0.999 or ym in months:
                continue
            months[ym] = round(today / (1.0 + p), 2)
    return [(ym, round(float(v), 2)) for ym, v in sorted(months.items())]


def year_matrix(months):
    """[("2026-11", 22.25)] → {(2026, 11): 22.25}"""
    mat = {}
    for ym, v in months:
        try:
            mat[(int(ym[:4]), int(ym[5:7]))] = v
        except (ValueError, IndexError):
            continue
    return mat


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


def clear_old_charts(sh):
    """把表里所有旧图删掉（只做一次，两个市场共用一张隐藏数据页）"""
    meta = api(sh.fetch_sheet_metadata,
               {"fields": "sheets(properties(sheetId,title),charts(chartId))"})
    old = [ch["chartId"] for s in meta.get("sheets", [])
           for ch in (s.get("charts") or [])]
    if old:
        api(sh.batch_update, {"requests": [{"deleteEmbeddedObject": {"objectId": c}}
                                           for c in old]})
        log("  清掉旧图 %d 张" % len(old))
    return meta


def write_pool(sh, items):
    """隐藏表：每条报价一段 [月份, 月均价]，返回每段 0-based 行范围"""
    total = sum(1 + len(ms) for _, ms, _, _ in items) + 20
    ws = ensure_sheet(sh, TAB_POOL, total, 2)
    api(ws.clear)
    buf, spans = [], []
    for key, ms, label, _ in items:
        start = len(buf)
        buf.append(["月份", label])
        for ym, v in ms:
            buf.append([ym, v])
        spans.append((key, start, len(buf)))
    for i in range(0, len(buf), BATCH):
        api(ws.update, buf[i:i + BATCH], "A%d" % (i + 1), value_input_option="RAW")
    api(sh.batch_update, {"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}]})
    log("「%s」写入 %d 行月均价数据源（已隐藏）" % (TAB_POOL, len(buf)))
    return ws, spans


def chart_req(pool_id, trend_id, r0, r1, anchor_row, unit):
    return {"addChart": {"chart": {
        "spec": {
            "title": "",
            "hiddenDimensionStrategy": "SHOW_ALL",
            "basicChart": {
                "chartType": "LINE",
                "legendPosition": "TOP_LEGEND",
                "axis": [{"position": "BOTTOM_AXIS", "title": "月份"},
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


def block_h(mats):
    """每块占多少行：标题 + 图 + 「历史月均价」表（年份数决定）"""
    ymax = max([len({y for y, _ in m}) for _, _, m, _ in mats] + [1])
    return DATA_OFF + min(ymax, MAX_YEARS) + 1


def write_trend(sh, pool_id, span_map, items, stamp, prefix, market):
    """一个市场的走势页，每块 = 标题 + 月价格折线图 + 年×月 月均价矩阵（照康美线排版）"""
    mats = [(key, rec, year_matrix(ms), ms) for key, ms, label, rec in items]
    labels = {key: label for key, _, label, _ in items}
    bh = block_h(mats)
    pages = max(1, (len(items) + PER_PAGE - 1) // PER_PAGE)
    anchors, charts = {}, 0

    for p in range(pages):
        title = "%s%d" % (prefix, p + 1)
        chunk = mats[p * PER_PAGE:(p + 1) * PER_PAGE]
        total = 1 + len(chunk) * bh + 5
        ws = ensure_sheet(sh, title, total, COLS_TREND)
        api(sh.batch_update, {"requests": [{"updateCells": {
            "range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}}]})
        api(ws.clear)

        area = [[""] * COLS_TREND for _ in range(total)]
        area[0][0] = ("数据更新时间：%s ｜ 数据来源：中香网 zhongxiangwang.cn ｜ %s市场 ｜ "
                      "月均价 = 当月日价平均（2026-11 起累积，首月含按官方涨跌幅反推的年初/月初价）"
                      " ｜ 第 %d/%d 页" % (stamp, market, p + 1, pages))

        reqs, bold = [], []
        for idx, (key, rec, mat, ms) in enumerate(chunk):
            label = labels.get(key, key.replace("|", " "))
            bs = 1 + idx * bh

            if ms:
                area[bs][0] = "月价格走势图：%s" % label
                area[bs][8] = "最新月均价：%.2f %s" % (ms[-1][1], rec["unit"] or "")
            else:
                area[bs][0] = "月价格走势图：%s（暂无数据）" % label
            bold.append(bs)

            area[bs + TABLE_TITLE_OFF][0] = "历史月均价：%s" % label
            bold.append(bs + TABLE_TITLE_OFF)

            head = bs + TABLE_HEAD_OFF
            area[head][0] = "年/月"
            for i, mn in enumerate(MONTH_NAMES, start=1):
                area[head][i] = mn
            bold.append(head)

            for j, y in enumerate(sorted({y for y, _ in mat})[:MAX_YEARS]):
                r = bs + DATA_OFF + j
                area[r][0] = "%d年" % y
                for mo in range(1, 13):
                    v = mat.get((y, mo))
                    area[r][mo] = "—" if v is None else round(float(v), 2)

            r0, r1 = span_map[key]
            if ms:
                reqs.append(chart_req(pool_id, ws.id, r0, r1, bs + CHART_OFF, rec["unit"]))
                charts += 1
            anchors[key] = (ws.id, "A%d" % (bs + 1))

        api(ws.update, area, "A1", value_input_option="RAW")

        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": total,
                      "startColumnIndex": 1, "endColumnIndex": COLS_TREND},
            "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER",
                                                           "pattern": "0.00"}}},
            "fields": "userEnteredFormat.numberFormat"}})
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 260}, "fields": "pixelSize"}})
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": ws.id, "dimension": "COLUMNS",
                      "startIndex": 1, "endIndex": COLS_TREND},
            "properties": {"pixelSize": 78}, "fields": "pixelSize"}})
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

    log("「%s」共 %d 页 / %d 块 / %d 张图" % (prefix, pages, len(items), charts))
    return anchors, pages


def drop_stale_tabs(sh, keep):
    """删掉旧版页（香料价格 / 香料走势*）以及本次没用到、上次留下的走势页"""
    meta = api(sh.fetch_sheet_metadata, {"fields": "sheets(properties(sheetId,title,hidden))"})
    sheets = [(s["properties"].get("title", ""), s["properties"]["sheetId"],
               bool(s["properties"].get("hidden"))) for s in meta.get("sheets", [])]
    visible = [t for t, _, h in sheets if not h]
    victims = []
    for title, gid, hidden in sheets:
        if hidden or title == TAB_POOL or title in keep:
            continue
        if title in OLD_TABS_EXACT or any(title.startswith(p) for p in OLD_TABS_PREFIX):
            victims.append((title, gid))
    for title, gid in victims:
        if len(visible) <= 1:
            log("  只剩一页可见，停止删除「%s」" % title)
            break
        try:
            api(sh.batch_update, {"requests": [{"deleteSheet": {"sheetId": gid}}]})
            visible.remove(title)
            log("  删掉旧页「%s」" % title)
        except Exception as e:
            log("  跳过删除「%s」：%s" % (title, e))


def reorder(sh, order):
    meta = api(sh.fetch_sheet_metadata, {"fields": "sheets(properties(sheetId,title))"})
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    reqs = [{"updateSheetProperties": {"properties": {"sheetId": ids[n], "index": i},
                                       "fields": "index"}}
            for i, n in enumerate(order) if n in ids]
    if reqs:
        api(sh.batch_update, {"requests": reqs})
        log("工作表顺序 → %s" % " / ".join(order))


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

    # 两张表各自不带「市场」列（表名已经说明），在「更新时间」前插一列「历史价格」
    out_header = [c for c in header if c != "市场"]
    if LINK_COL not in out_header:
        out_header.insert(out_header.index("更新时间"), LINK_COL)
    insert_at = out_header.index(LINK_COL)

    # 按市场分组，同一种品类两边都有时各进各的表
    grouped = {m: [] for m, _, _ in MARKETS}
    display = {m: [] for m, _, _ in MARKETS}
    all_items = []
    for r in rows:
        name, producer, spec = cell(r, "品类"), cell(r, "产地"), cell(r, "规格")
        market = cell(r, "市场") or "国内"
        if market not in grouped:
            market = "国内"
        key = key_of(name, producer, spec, market)
        rec = {
            "date": qdate,
            "unit": cell(r, "单位"),
            "today": num(cell(r, "今日价")),
            "prev": num(cell(r, "前日价")),
            "dod": pct(cell(r, "较前日")),
            "mom": pct(cell(r, "较月初")),
            "yoy": pct(cell(r, "较年初")),
        }
        label = "%s · %s · %s" % (name, producer, spec)
        item = (key, months_of(key, rec, hist), label, rec)
        grouped[market].append(item)
        all_items.append(item)

        # 明细行：去掉「市场」列，并在「历史价格」位置留空，稍后写 HYPERLINK
        rr = [c for i0, c in enumerate(r) if header[i0] != "市场"]
        rr = (rr + [""] * len(out_header))[:len(out_header)]
        rr.insert(insert_at, "")
        display[market].append(rr)

    for m, _, _ in MARKETS:
        log("%s：%d 行" % (m, len(grouped[m])))

    up = sum(1 for r in rows if (num(cell(r, "日涨跌")) or 0) > 0)
    down = sum(1 for r in rows if (num(cell(r, "日涨跌")) or 0) < 0)
    log("明细合计 %d 行 × %d 列；日涨跌 涨 %d / 跌 %d / 平 %d"
        % (len(rows), len(out_header), up, down, len(rows) - up - down))

    if a.dry_run:
        for m, tab, prefix in MARKETS:
            pages = max(1, (len(grouped[m]) + PER_PAGE - 1) // PER_PAGE)
            sample = grouped[m][0] if grouped[m] else None
            log("  预览：%s %d 行 → %s %d 页%s" % (
                tab, len(grouped[m]), prefix, pages,
                ("；样例 %s 月点=%d" % (sample[2], len(sample[1]))) if sample else ""))
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

    clear_old_charts(sh)
    pool, spans = write_pool(sh, all_items)
    span_map = {k: (a0, b0) for k, a0, b0 in spans}

    # 先写走势页（拿到 gid 和锚点），再写价格表（「历史价格」列要链过去）
    anchors, trend_order, price_tabs = {}, [], []
    for m, tab_price, tab_trend in MARKETS:
        a_map, pages = write_trend(sh, pool.id, span_map, grouped[m], stamp, tab_trend, m)
        anchors.update(a_map)
        trend_order += ["%s%d" % (tab_trend, p + 1) for p in range(pages)]
        price_tabs.append(tab_price)

    for m, tab_price, tab_trend in MARKETS:
        links = []
        for key, ms, label, rec in grouped[m]:
            gid, anchor = anchors.get(key, ("", "A1"))
            url = ("https://docs.google.com/spreadsheets/d/%s/edit#gid=%s&range=%s"
                   % (sid, gid, anchor))
            links.append(['=HYPERLINK("%s","%s")' % (url, LINK_TEXT)])

        ws = write_table(sh, tab_price, out_header, display[m])
        if links:
            api(ws.update, links, "%s2" % col_letter(insert_at + 1),
                value_input_option="USER_ENTERED")
        log("「%s」写入 %d 个历史价格跳转链接" % (tab_price, len(links)))

    drop_stale_tabs(sh, keep=set(price_tabs + trend_order + [TAB_POOL]))
    reorder(sh, price_tabs + trend_order + [TAB_POOL])
    log("全部完成 → https://docs.google.com/spreadsheets/d/%s/edit" % sid)


if __name__ == "__main__":
    main()
