# -*- coding: utf-8 -*-
r"""
把康美香料价格推到 Google 表格（每天全量覆盖）

表格：sheet_id_spice.txt 里那一份（也可以给环境变量 SHEET_ID_SPICE）
    · 香料价格 —— 第一页。明细：品名/规格/产地 + 亳州 安国 成都 玉林 廉桥 普宁 六市场价
                  + 最高/最低/均价 + 周月季年涨跌 + 走势 + 历史价格(跳转) + 更新时间
    · 香料走势 —— 每味一块，照康美官网那一版排版：
                  ① 标题「月价格走势图：品名（规格/产地）」+ 右上「最新月价格：xx 元/千克」
                  ② 月价格折线图（横轴月份，纵轴元/千克）
                  ③ 「历史月均价：品名（规格/产地）」年 × 月 矩阵表（十二列，空格写 —）
                  「香料价格」里的「历史价格」列就跳到对应这一块。

数据来源：km_spice.py（跑在阿里云函数上抓 kmzyw.com.cn，抓完提交 CSV/JSON 回仓库）
    km_spice_price.csv   明细
    km_spice_hist.json.gz 走势 + 月价格

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

from km_spice import MARKETS, PRICE_HEADER          # noqa: F401
from supplier_push import BATCH, connect, ensure_sheet, log

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_PRICE = os.path.join(BASE_DIR, "km_spice_price.csv")
JSON_HIST = os.path.join(BASE_DIR, "km_spice_hist.json")
SHEET_ID_FILE = os.path.join(BASE_DIR, "sheet_id_spice.txt")

TAB_PRICE = "香料价格"
TAB_TREND = "香料走势"
TAB_POOL = "走势数据"
TAB_OLD_RANK = "香料涨跌榜"      # 旧版有这一页，现在不要了，跑的时候顺手删掉

# 明细表里的「历史价格」列：跟药通网一样放一个链接，点开就是这味的月价格曲线
LINK_COL = "历史价格"
LINK_TEXT = "点击查看"

MONTH_NAMES = ["一月", "二月", "三月", "四月", "五月", "六月",
               "七月", "八月", "九月", "十月", "十一月", "十二月"]

# 走势页每味一块的排版（相对块首行的偏移，0-based）
CHART_OFF = 1          # 图贴在第 2 行
TABLE_TITLE_OFF = 18   # 「历史月均价：…」
TABLE_HEAD_OFF = 19    # 年/月 + 一月…十二月
DATA_OFF = 20          # 数据从这一行开始
CHART_W, CHART_H = 980, 300
MAX_YEARS = 8          # 每味最多列 8 个年份（实测 2023~2026 四年）

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


def load():
    if not os.path.exists(CSV_PRICE):
        raise SystemExit("没找到 %s，先跑 km_spice.py" % CSV_PRICE)
    with open(CSV_PRICE, encoding="utf-8-sig", newline="") as f:
        rd = csv.reader(f)
        header = next(rd, None) or []
        rows = [r for r in rd if r and any(c.strip() for c in r)]
    # 走势：仓库里存的是 gz（阿里云函数提交），本机跑时可能是 .json
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


def year_matrix(months):
    """[(YYYY-MM, 价)] → {(年, 月): 价}，给「历史月均价」矩阵表用"""
    d = {}
    for m, p in months:
        d[(int(m[:4]), int(m[5:7]))] = p
    return d


def sheet_or_none(sh, title):
    try:
        return sh.worksheet(title)
    except gspread.WorksheetNotFound:
        return None


def col_letter(n):
    """1 → A，3 → C，27 → AA"""
    s = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


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

    每段的表头第二格写成「品名（规格/产地）」，这样图上的图例就是这味药的名字。
    """
    total = sum(1 + len(months_of(rec)) for _, rec in items) + 20
    ws = ensure_sheet(sh, TAB_POOL, total, 2)
    ws.clear()
    buf, spans = [], []
    for key, rec in items:
        name = key.replace("|", "  ")
        start = len(buf)
        buf.append(["月份", name])
        for m, p in months_of(rec):
            buf.append([m, p])
        spans.append((key, start, len(buf)))       # endRowIndex 是开区间
    for i in range(0, len(buf), BATCH):
        api(ws.update, buf[i:i + BATCH], "A%d" % (i + 1), value_input_option="RAW")
    api(sh.batch_update, {"requests": [{"updateSheetProperties": {
        "properties": {"sheetId": ws.id, "hidden": True}, "fields": "hidden"}}]})
    log("「%s」写入 %d 行月价格数据源（已隐藏）" % (TAB_POOL, len(buf)))
    return ws, spans


def chart_req(pool_id, trend_id, r0, r1, anchor_row):
    """一张月价格折线图：数据源是隐藏表里那一味的月价格段，贴在走势页对应块"""
    return {"addChart": {"chart": {
        "spec": {
            "title": "",
            # 数据源表是隐藏的，必须显式声明读隐藏行列，否则图出来是空的
            "hiddenDimensionStrategy": "SHOW_ALL",
            "basicChart": {
                "chartType": "LINE",
                "legendPosition": "TOP_LEGEND",
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
        "position": {"overlayPosition": {
            "anchorCell": {"sheetId": trend_id, "rowIndex": anchor_row, "columnIndex": 0},
            "widthPixels": CHART_W, "heightPixels": CHART_H}},
    }}}


def write_trend(sh, items, stamp):
    """走势页：每味一块 = 标题 + 最新月价 + 折线图 + 年×月历史月均价表

    版式照康美官网「月价格走势图 + 历史月均价」那一页。
    这一页同时充当「香料价格」里「历史价格」的超链接目标，所以块的行号必须
    和明细行顺序严格一致（都按 品名|规格|产地 排序）。
    """
    mats = [(key, rec, year_matrix(months_of(rec))) for key, rec in items]
    ymax = max([len({y for y, _ in m}) for _, _, m in mats] + [1])
    ymax = min(ymax, MAX_YEARS)
    block_h = DATA_OFF + ymax + 1
    total = 1 + len(mats) * block_h + 5

    ws = ensure_sheet(sh, TAB_TREND, total, 13)
    api(sh.batch_update, {"requests": [{"updateCells": {
        "range": {"sheetId": ws.id}, "fields": "userEnteredFormat"}}]})
    ws.clear()

    pool, spans = write_pool(sh, items)
    span_map = {k: (a, b) for k, a, b in spans}

    area = [[""] * 13 for _ in range(total)]
    area[0][0] = ("数据更新时间：%s（北京时间） ｜ 数据来源：康美中药网 kmzyw.com.cn ｜ 单位：元/千克"
                  % stamp)

    reqs, anchors, bold_rows = [], [], []
    charts_made = 0
    for idx, (key, rec, mat) in enumerate(mats):
        name = key.replace("|", "  ")
        bs = 1 + idx * block_h
        ms = months_of(rec)

        area[bs][0] = "月价格走势图：%s" % name
        if ms:
            area[bs][8] = "最新月价格：%.2f 元/千克" % ms[-1][1]
        else:
            # 檀香白/檀香黄/沉香含量15 这类进口规格，康美本来就没有月价格曲线
            area[bs][0] = "月价格走势图：%s（康美暂无月价格数据）" % name
        bold_rows.append(bs)

        area[bs + TABLE_TITLE_OFF][0] = "历史月均价：%s" % name
        bold_rows.append(bs + TABLE_TITLE_OFF)

        head = bs + TABLE_HEAD_OFF
        area[head][0] = "年/月"
        for i, mn in enumerate(MONTH_NAMES, start=1):
            area[head][i] = mn
        bold_rows.append(head)

        for j, y in enumerate(sorted({y for y, _ in mat})):
            r = bs + DATA_OFF + j
            area[r][0] = "%d年" % y
            for mo in range(1, 13):
                v = mat.get((y, mo))
                area[r][mo] = "—" if v is None else round(float(v), 2)

        r0, r1 = span_map[key]
        if ms:
            reqs.append(chart_req(pool.id, ws.id, r0, r1, bs + CHART_OFF))
            charts_made += 1
        anchors.append("A%d" % (bs + 1))

    api(ws.update, area, "A1", value_input_option="RAW")

    # 数字格式：B..M 列一律两位小数（「—」是文本，不受影响）
    reqs.append({"repeatCell": {
        "range": {"sheetId": ws.id, "startRowIndex": 1, "endRowIndex": total,
                  "startColumnIndex": 1, "endColumnIndex": 13},
        "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.00"}}},
        "fields": "userEnteredFormat.numberFormat"}})
    # 列宽：A 放标题宽一点，B..M 十二个月
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
        "properties": {"pixelSize": 260}, "fields": "pixelSize"}})
    reqs.append({"updateDimensionProperties": {
        "range": {"sheetId": ws.id, "dimension": "COLUMNS", "startIndex": 1, "endIndex": 13},
        "properties": {"pixelSize": 78}, "fields": "pixelSize"}})
    for bs in bold_rows:
        reqs.append({"repeatCell": {
            "range": {"sheetId": ws.id, "startRowIndex": bs, "endRowIndex": bs + 1,
                      "startColumnIndex": 0, "endColumnIndex": 13},
            "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
            "fields": "userEnteredFormat.textFormat"}})

    # 旧图清理：走势页 + 隐藏数据页上的图都删掉（每天全量重建）
    # 早期版本把图贴在「走势数据」页上，不清掉会一天天堆图
    meta = api(sh.fetch_sheet_metadata,
               {"fields": "sheets(properties(sheetId,title),charts(chartId))"})
    keep_id = {ws.id, pool.id}
    old = []
    for s in meta.get("sheets", []):
        if s.get("properties", {}).get("sheetId") not in keep_id:
            continue
        for ch in s.get("charts", []) or []:
            old.append(ch["chartId"])
    if old:
        api(sh.batch_update, {"requests": [{"deleteEmbeddedObject": {"objectId": cid}}
                                           for cid in old]})
        log("  清掉旧图 %d 张" % len(old))
    for i in range(0, len(reqs), 100):
        api(sh.batch_update, {"requests": reqs[i:i + 100]})
    log("「%s」%d 味：月价格图 %d 张 + 历史月均价表" % (TAB_TREND, len(mats), charts_made))
    return ws, anchors


def reorder(sh, order):
    """按给定顺序排工作表（第 0 个排最前）"""
    meta = api(sh.fetch_sheet_metadata, {"fields": "sheets(properties(sheetId,title))"})
    ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta.get("sheets", [])}
    reqs = []
    for i, name in enumerate(order):
        if name in ids:
            reqs.append({"updateSheetProperties": {
                "properties": {"sheetId": ids[name], "index": i}, "fields": "index"}})
    if reqs:
        api(sh.batch_update, {"requests": reqs})
        log("工作表顺序 → " + " / ".join(n for n in order if n in ids))


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

    # 走势/月价格：按「品名|规格|产地」排序，明细表也按同一顺序写，
    # 这样第 n 味 ↔ 走势页第 n 块，超链接才对得上。
    def key_of(r):
        return "%s|%s|%s" % (cell(r, "品名"), cell(r, "规格"), cell(r, "产地"))

    items = sorted(((key_of(r), hist[key_of(r)]) for r in rows if key_of(r) in hist),
                   key=lambda x: x[0])

    def num(r, c):
        try:
            return float(cell(r, c))
        except ValueError:
            return None

    up = sum(1 for r in rows if (num(r, "周涨跌") or 0) > 0)
    down = sum(1 for r in rows if (num(r, "周涨跌") or 0) < 0)
    log("明细 %d 行 × %d 列；走势 %d 味；周涨跌 涨 %d / 跌 %d"
        % (len(rows), len(header), len(items), up, down))
    if rows:
        log("样例：%s / %s / %s  周涨跌 %s"
            % (cell(rows[0], "品名"), cell(rows[0], "规格"),
               cell(rows[0], "产地"), cell(rows[0], "周涨跌") or "—"))

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

    # 旧的「香料涨跌榜」整页删掉（用户 2026-11-18 明确不要这一页）
    old_rank = sheet_or_none(sh, TAB_OLD_RANK)
    if old_rank is not None:
        api(sh.del_worksheet, old_rank)
        log("删掉旧的「%s」工作表" % TAB_OLD_RANK)

    trend_ws, anchors = write_trend(sh, items, stamp)

    # 「历史价格」列：放在「更新时间」前一列，每行一个可点的超链接，跳到走势页对应那味
    # 明细行顺序 = items 顺序（都按 品名|规格|产地 排），所以第 i 行对第 i 块
    links = None
    if LINK_COL in header:
        col = header.index(LINK_COL)
        links = []
        for i in range(len(rows)):
            anchor = anchors[i] if i < len(anchors) else "A1"
            url = ("https://docs.google.com/spreadsheets/d/%s/edit#gid=%d&range=%s"
                   % (sid, trend_ws.id, anchor))
            links.append(['=HYPERLINK("%s","%s")' % (url, LINK_TEXT)])

    ws_price = write_table(sh, TAB_PRICE, header, rows)
    if links:
        api(ws_price.update, links, "%s2" % col_letter(col + 1),
            value_input_option="USER_ENTERED")
        log("「%s」写入 %d 个历史价格跳转链接（末列之前）" % (TAB_PRICE, len(links)))

    reorder(sh, [TAB_PRICE, TAB_TREND, TAB_POOL])
    log("全部完成 → https://docs.google.com/spreadsheets/d/%s/edit" % sid)


if __name__ == "__main__":
    main()
