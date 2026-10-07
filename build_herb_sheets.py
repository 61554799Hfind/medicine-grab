# -*- coding: utf-8 -*-
r"""
药材历史价格「大图」表：每块 = 标题 + 大折线图 + 年×月价格表
------------------------------------------------------------------
给老板看的工作簿里只保留两类表：
    medicine_data（主表）
    历史图1 / 历史图2 ... （每卷 50 个药材，每块一张官网样式大折线图）
逐日原始数据放在 `daily_pool`（**默认隐藏**，老板看不到），图表跨表引用它。

为什么必须有 daily_pool：
  Google 单工作簿上限约 1000 万格，且**空格子也算**。
  926 个药材 × 1097 个日价格点，如果数据塞进卷表内，
  卷表列数（13 列）会把每行都撑满 → 约 1350 万格，超限。
  单独拿一张 2 列的表竖着堆 → 只要 200 万格，安全。

每次运行都会**先删掉旧的「历史图*」和池表再重建**，
所以每天自动更新时不会残留昨天的图（覆盖式更新，不是叠加）。

卷表布局（每块 22 行）：
    第 R 行    : 标题「品名 规格 产地 — 历史价格走势（起止）」
    第 R+1 行  : 【大折线图】900×280px，逐日价格
    第 R+16 行 : 年份 | 1月 | 2月 … 12月
    第 R+17~   : 2023 / 2024 / 2025 / 2026 各一行

用法：
    .venv\Scripts\python.exe build_herb_sheets.py --dry-run                # 只算布局，不连 Google
    .venv\Scripts\python.exe build_herb_sheets.py --limit 10               # 先建 10 个看效果
    .venv\Scripts\python.exe build_herb_sheets.py --per-sheet 50 --drop-monthly   # 全量（926 个）
"""
import argparse
import json
import math
import os
from collections import defaultdict

import gspread

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(BASE_DIR, "history_data.json")

from push_history import (
    SHEET_ID, SERVICE_ACCOUNT_FILE, _run_with_proxy, col_letter,
)

try:
    GSPREAD_V6 = int(gspread.__version__.split(".")[0]) >= 6
except Exception:
    GSPREAD_V6 = False


def _update_range(ws, a1, values, vio="RAW"):
    """兼容 gspread 5.x / 6.x 的写入写法"""
    if GSPREAD_V6:
        ws.update(values, a1, value_input_option=vio)
    else:
        ws.update(a1, values, value_input_option=vio)


MONTH_HEADERS = [f"{m}月" for m in range(1, 13)]

BLOCK_H = 22          # 每个药材占的行数
CHART_ROW_OFF = 1     # 图表锚定在块内第几行（相对块首行，0-based）
TABLE_ROW_OFF = 16    # 年×月表头在块内第几行（0-based）
CHART_W, CHART_H = 900, 280
VOL_COLS = 14         # 卷表列数 A..N
VOL_PREFIX = "历史图"
POOL_NAME = "daily_pool"     # 逐日原始数据池（默认隐藏）
MAX_POOL_ROWS = 500_000      # 单张池表最多行数（超限自动拆成 daily_pool_2 ...）
POOL_CHUNK = 25_000          # 每次 API 写入的行数
CHART_BATCH = 25             # 每批 addChart 请求数
MAX_SHEETS = 200
CELL_LIMIT = 10_000_000

# 每次运行都要清掉的旧表（保证覆盖式更新，不残留昨天的图）
DROP_ALWAYS = ("history_daily", "history_index")

PREFERRED = ["三七", "丹参", "当归", "黄芪", "枸杞子", "党参", "白术", "茯苓",
             "甘草", "陈皮", "白芍", "半夏", "金银花", "连翘", "板蓝根"]


def pick_combos(raw, limit):
    keys = sorted(raw.keys())
    chosen = []
    for want in PREFERRED:
        for k in keys:
            if k.split("|")[0] == want and k not in chosen:
                chosen.append(k)
                break
        if len(chosen) >= limit:
            break
    for k in keys:
        if len(chosen) >= limit:
            break
        if k not in chosen:
            chosen.append(k)
    return chosen[:limit]


def build_year_month(pts):
    """年 -> {月: 该月15日价}（口径同官网）"""
    table = defaultdict(dict)
    for d, p in pts:
        if not d or len(d) < 10:
            continue
        try:
            y, m, day, v = int(d[:4]), int(d[5:7]), int(d[8:10]), float(p)
        except Exception:
            continue
        if m not in table[y] and day >= 15:
            table[y][m] = round(v, 2)
    return table


def plan_pool(combos, raw):
    """把药材按行分配到若干张池表。返回 (pools=[[key..], ..], offs={key:(池序号,start0,end0)})"""
    pools, offs = [], {}
    cur, cur_rows = [], 0
    for key in combos:
        n = 1 + len(raw[key])          # 含 1 行表头
        if cur and cur_rows + n > MAX_POOL_ROWS:
            pools.append(cur)
            cur, cur_rows = [], 0
        offs[key] = (len(pools), cur_rows, cur_rows + n)
        cur.append(key)
        cur_rows += n
    if cur:
        pools.append(cur)
    return pools, offs


def cleanup_old(sh, drop_monthly):
    """删除上一轮留下的卷表/池表/废弃表 —— 保证是覆盖更新，不是叠加"""
    targets = list(DROP_ALWAYS)
    if drop_monthly:
        targets.append("history_monthly")
    removed = []
    for ws in list(sh.worksheets()):
        t = ws.title
        if (t == POOL_NAME or t.startswith(POOL_NAME + "_")
                or t.startswith(VOL_PREFIX)
                or t in targets):
            sh.del_worksheet(ws)
            removed.append(t)
    if removed:
        print(f"  🧹 已删除上一轮的 {len(removed)} 张表：{'、'.join(removed)}")
    else:
        print("  🧹 没有需要清理的旧表")


def write_pool(sh, combos, raw, hide=True):
    """建池表，写入所有药材的逐日数据（堆叠）。返回 (pool_ids=[sheetId..], offs)"""
    pools, offs = plan_pool(combos, raw)
    ids = []
    for i, keys in enumerate(pools):
        title = POOL_NAME if i == 0 else f"{POOL_NAME}_{i + 1}"
        rows = sum(1 + len(raw[k]) for k in keys) + 2
        ws = sh.add_worksheet(title=title, rows=rows, cols=2)
        ids.append(ws.id)

        buf, base = [], 0

        def flush():
            nonlocal buf, base
            if not buf:
                return
            a1 = f"A{base + 1}:B{base + len(buf)}"
            _update_range(ws, a1, buf, "USER_ENTERED")   # USER_ENTERED：日期变真日期，横轴才正常
            base += len(buf)
            buf = []

        for k in keys:
            buf.append(["日期", "价格"])
            for d, p in raw[k]:
                buf.append([d, p])
            if len(buf) >= POOL_CHUNK:
                flush()
        flush()
        print(f"  ✅ {title}：{len(keys)} 个药材 / {rows - 2:,} 行 × 2 列")

    if hide:
        sh.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": pid, "hidden": True},
                "fields": "hidden",
            }
        } for pid in ids]})
        print("  🙈 数据源表已隐藏（老板看不到）")
    return ids, offs


def plan_volume(part, raw, pool_off, pool_ids):
    """构造一卷的区块内容 + 图表参数"""
    block_area, specs = [], []
    for idx, key in enumerate(part):
        name, spec, origin = key.split("|", 2)
        pts = raw[key]
        bs = idx * BLOCK_H                       # 0-based 块首行
        while len(block_area) < bs + BLOCK_H:
            block_area.append([""] * 13)

        span = f"{pts[0][0]} ~ {pts[-1][0]}" if pts else ""
        block_area[bs][0] = f"{name} {spec} {origin} — 历史价格走势（{span}）"

        ym = build_year_month(pts)
        block_area[bs + TABLE_ROW_OFF - 1] = ["年份"] + MONTH_HEADERS
        for j, y in enumerate(sorted(ym.keys())):
            if bs + TABLE_ROW_OFF + j < len(block_area):
                block_area[bs + TABLE_ROW_OFF + j] = \
                    [y] + [ym[y].get(m, "") for m in range(1, 13)]

        pi, p0, p1 = pool_off[key]
        specs.append({
            "key": key, "title": f"{name} {spec} {origin} 价格走势",
            "anchor_row": bs + CHART_ROW_OFF, "block_start": bs,
            "pool_id": pool_ids[pi], "p0": p0, "p1": p1,
        })
    return block_area, specs


def create_volumes(sh, combos, raw, per_sheet, drop_monthly=False, hide_pool=True):
    cleanup_old(sh, drop_monthly)

    print("  写数据源 daily_pool ...")
    pool_ids, pool_off = write_pool(sh, combos, raw, hide=hide_pool)

    n_vol = math.ceil(len(combos) / per_sheet)
    if n_vol + len(pool_ids) + 5 > MAX_SHEETS:
        raise SystemExit(
            f"需要 {n_vol} 张卷表 + {len(pool_ids)} 张池表，接近/超过 Google 上限 {MAX_SHEETS}，"
            f"请调大 --per-sheet")

    combo_gid = {}
    vol_cells = 0
    for v in range(n_vol):
        part = combos[v * per_sheet:(v + 1) * per_sheet]
        title = f"{VOL_PREFIX}{v + 1}" if n_vol > 1 else VOL_PREFIX

        block_area, specs = plan_volume(part, raw, pool_off, pool_ids)
        n_rows = len(block_area) + 5
        ws = sh.add_worksheet(title=title, rows=n_rows, cols=VOL_COLS)
        sid = ws.id
        vol_cells += n_rows * VOL_COLS
        _update_range(ws, "A1", block_area, "RAW")

        reqs = []
        for s in specs:
            combo_gid[s["key"]] = (sid, s["block_start"] + 1)
            reqs.append({
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": s["title"],
                            # ⚠️ hiddenDimensionStrategy 是 ChartSpec 的字段，不是 basicChart 的。
                            # 放错层级会直接 400：Unknown name "hiddenDimensionStrategy" at ...basic_chart
                            # 作用：数据源表 daily_pool 是隐藏的，显式声明不跳过隐藏行列，避免图变空。
                            "hiddenDimensionStrategy": "SHOW_ALL",
                            "basicChart": {
                                "chartType": "LINE",
                                "legendPosition": "NO_LEGEND",
                                "axis": [
                                    {"position": "BOTTOM_AXIS", "title": "日期"},
                                    {"position": "LEFT_AXIS", "title": "价格(元/kg)"},
                                ],
                                "domains": [{
                                    "domain": {"sourceRange": {"sources": [{
                                        "sheetId": s["pool_id"],
                                        "startRowIndex": s["p0"], "endRowIndex": s["p1"],
                                        "startColumnIndex": 0, "endColumnIndex": 1,
                                    }]}}
                                }],
                                "series": [{
                                    "series": {"sourceRange": {"sources": [{
                                        "sheetId": s["pool_id"],
                                        "startRowIndex": s["p0"], "endRowIndex": s["p1"],
                                        "startColumnIndex": 1, "endColumnIndex": 2,
                                    }]}},
                                    "targetAxis": "LEFT_AXIS",
                                }],
                                "headerCount": 1,
                            },
                        },
                        "position": {"overlayPosition": {
                            "anchorCell": {"sheetId": sid, "rowIndex": s["anchor_row"],
                                           "columnIndex": 0},
                            "widthPixels": CHART_W, "heightPixels": CHART_H,
                        }},
                    }
                }
            })

        for i in range(0, len(reqs), CHART_BATCH):
            sh.batch_update({"requests": reqs[i:i + CHART_BATCH]})
        print(f"  ✅ {title}：{len(part)} 个药材 / {len(reqs)} 个大图 / {n_rows} 行")

    # ---- 自检：回读确认图表真的绑上了数据（防止出现「空图」）----
    try:
        meta = sh.fetch_sheet_metadata(
            {"fields": "sheets(properties(title),charts(chartSpec(basicChart(series(series(sourceRange(sources)))))))"})
        ok = 0
        for s in meta.get("sheets", []):
            for ch in s.get("charts", []):
                series = (ch.get("chartSpec", {}).get("basicChart", {}) or {}).get("series", [])
                for se in series:
                    srcs = ((se.get("series", {}) or {}).get("sourceRange", {}) or {}).get("sources", [])
                    if srcs:
                        ok += 1
        print(f"  🔎 自检：共 {ok} 个图表已绑定数据源（期望 {len(combos)}）")
        if ok == 0:
            print("  ⚠️ 警告：没有图表绑定到数据，请把这段输出发给我")
    except Exception as e:
        print(f"  （自检跳过：{e}）")

    pool_rows = sum(1 + len(raw[k]) for k in combos)
    total_new = pool_rows * 2 + vol_cells
    print(f"  占用约 {total_new:,} 格（池 {pool_rows * 2:,} + 卷 {vol_cells:,}）｜上限 {CELL_LIMIT:,}")
    if total_new > CELL_LIMIT * 0.8:
        print("  ⚠️ 接近单元格上限，建议调大 --per-sheet 或减小药材数")
    return combo_gid


def ensure_link_column(sh):
    """确保 medicine_data 有「历史价格」列，返回列号（1-based）"""
    wd = sh.worksheet("medicine_data")
    header = wd.row_values(1)
    if "历史价格" in header:
        return header.index("历史价格") + 1
    idx = header.index("更新日期") if "更新日期" in header else len(header)
    sh.batch_update({"requests": [{
        "insertDimension": {
            "range": {"sheetId": wd.id, "dimension": "COLUMNS",
                      "startIndex": idx, "endIndex": idx + 1},
            "inheritFromBefore": False,
        }
    }]})
    wd.update_cell(1, idx + 1, "历史价格")
    print(f"  ➕ medicine_data 缺「历史价格」列，已插到第 {idx + 1} 列")
    return idx + 1


def update_links(sh, combo_gid):
    hi = ensure_link_column(sh)
    wd = sh.worksheet("medicine_data")
    vals = wd.get_all_values()
    if len(vals) < 2:
        print("⚠️ medicine_data 是空的，跳过")
        return

    row_map, mgid = {}, None
    try:
        wm = sh.worksheet("history_monthly")
        mgid = wm.id
        for i, r in enumerate(wm.get_all_values()[1:], start=2):
            if len(r) >= 3:
                row_map[f"{r[0]}|{r[1]}|{r[2]}"] = i
    except gspread.WorksheetNotFound:
        pass

    formulas, hit, blank = [], 0, 0
    for i in range(2, len(vals) + 1):
        row = vals[i - 1]
        key = f"{row[0]}|{row[1]}|{row[2]}" if len(row) >= 3 else ""
        if key in combo_gid:
            sid, br = combo_gid[key]
            formulas.append([f'=HYPERLINK("#gid={sid}&range=A{br}","查看历史价格")'])
            hit += 1
        elif mgid is not None:
            tr = row_map.get(key, 1)
            formulas.append([f'=HYPERLINK("#gid={mgid}&range=A{tr}","查看历史价格")'])
        else:
            formulas.append([""])      # 没建图也没有兜底表 → 留空，不放死链
            blank += 1

    col = col_letter(hi)
    _update_range(wd, f"{col}2:{col}{len(vals)}", formulas, "USER_ENTERED")
    msg = f"✅ 链接列已更新：{hit} 行跳大图"
    if mgid is not None:
        msg += f"，其余 {len(vals) - 1 - hit - blank} 行仍跳 history_monthly"
    if blank:
        msg += f"，{blank} 行留空（本次没建图且已删除 history_monthly）"
    print(msg)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="建多少个药材（0=全部）")
    ap.add_argument("--per-sheet", type=int, default=50, help="每卷放几个药材")
    ap.add_argument("--drop-monthly", action="store_true",
                    help="连 history_monthly 一起删（全量建图后用它，工作簿只剩主表+历史图）")
    ap.add_argument("--show-pool", action="store_true",
                    help="不隐藏 daily_pool（默认隐藏，仅调试用）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(DATA_JSON, "r", encoding="utf-8") as f:
        raw = json.load(f)

    combos = pick_combos(raw, args.limit or len(raw))
    n_vol = math.ceil(len(combos) / args.per_sheet)
    pools, pool_off = plan_pool(combos, raw)
    vol_cells = sum((len(combos[v * args.per_sheet:(v + 1) * args.per_sheet]) * BLOCK_H + 5) * VOL_COLS
                    for v in range(n_vol))
    pool_rows = sum(1 + len(raw[k]) for k in combos)
    total_new = pool_rows * 2 + vol_cells

    print(f"共 {len(combos)} 个药材 → {n_vol} 张卷表（每卷 {args.per_sheet} 个）"
          f" + {len(pools)} 张隐藏池表"
          f"｜Google 表上限 {MAX_SHEETS} {'✅' if n_vol + len(pools) < MAX_SHEETS else '❌'}")
    print(f"占用估算：池 {pool_rows:,}行×2 = {pool_rows * 2:,} 格，"
          f"卷表共 {vol_cells:,} 格，合计约 {total_new:,} 格"
          f"｜上限 {CELL_LIMIT:,} {'✅' if total_new < CELL_LIMIT else '❌ 超限'}")
    print("每次运行先删旧卷表/池表再重建 → 每天更新是覆盖，不会残留昨天的图")

    if args.dry_run:
        part = combos[:args.per_sheet]
        ba, specs = plan_volume(part, raw, pool_off, [0] * len(pools))
        print(f"\n[dry-run] 第 1 卷：{len(part)} 个药材 / 区块 {len(ba)} 行 / {len(specs)} 图")
        s0 = specs[0]
        print(f"  第1块 标题行={s0['block_start'] + 1} 图表锚定行={s0['anchor_row'] + 1} "
              f"池#{s0['pool_id']} 行={s0['p0'] + 1}~{s0['p1']}")
        if len(specs) > 1:
            print(f"  第2块 标题行={specs[1]['block_start'] + 1}（间隔 {BLOCK_H} 行）")
        return

    from google.oauth2.service_account import Credentials
    import socket

    def work():
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(120)
        try:
            creds = Credentials.from_service_account_file(
                SERVICE_ACCOUNT_FILE,
                scopes=["https://www.googleapis.com/auth/spreadsheets"])
            client = gspread.authorize(creds)
            sh = client.open_by_key(SHEET_ID)
        finally:
            socket.setdefaulttimeout(old)
        gid = create_volumes(sh, combos, raw, args.per_sheet,
                             drop_monthly=args.drop_monthly,
                             hide_pool=not args.show_pool)
        update_links(sh, gid)

    if _run_with_proxy(work):
        print("\n🎉 完成。工作簿现在只剩：medicine_data + 历史图1…N（数据源 daily_pool 已隐藏）。")


if __name__ == "__main__":
    main()
