# -*- coding: utf-8 -*-
"""
把历史价格日数据写入 Google Sheet
------------------------------------------------------------------
依赖：history_crawl.py 产出的 history_data.json
      （结构：{ "品名|规格|产地": [["YYYY-MM-DD", "价格"], ...], ... }）

写入三个工作表（数据全部在表格内，不依赖外部网页）：
  1) history_daily   长格式：品名,规格,产地,日期,价格   （全量日级，满足「数据进表格」；可按药材筛选）
  2) history_index   品名,规格,产地,起始行,结束行        （索引，便于定位某品种在 history_daily 的位置）
  3) history_monthly 品名,规格,产地, YYYY-MM ..., 趋势   （月度价格网格 + 每行一个迷你折线图）
并在 medicine_data 写入一列「历史价格」：
  单元格是「查看历史价格」，点一下在表格内跳转到该品种在 history_monthly 的那一行
  （同一张表里就能看到该药材 3 年月度价格 + 走势小图）。

用法：
    .venv/Scripts/python.exe push_history.py --dry-run           # 只本地构建+统计
    .venv/Scripts/python.exe push_history.py --links-only        # 只刷新链接列（几秒）
    .venv/Scripts/python.exe push_history.py                     # 全量写入 GS
    .venv/Scripts/python.exe push_history.py --web-url https://xxx/index.html   # 改用网页跳转
"""
import argparse
import json
import os
import socket
from collections import defaultdict
from string import ascii_uppercase

import gspread
from google.oauth2.service_account import Credentials


def col_letter(i):
    """1->A, 26->Z, 27->AA, ..."""
    s = ""
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = ascii_uppercase[r] + s
    return s


def _month_val(vals):
    """取该月 15 日及以后第一个价格（口径同官网）；若该月无 15 日及以后数据则返回空。
    vals: 该月内 [(day, price), ...]，按日期升序。"""
    v15 = None
    for day, price in vals:
        if day >= 15:
            v15 = price
            break
    return round(v15, 2) if v15 is not None else ""

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_JSON = os.path.join(BASE_DIR, "history_data.json")
SHEET_ID_FILE = os.path.join(BASE_DIR, "sheet_id.txt")


def resolve_sheet_id():
    """解析目标表格 ID，优先级：环境变量 SHEET_ID > 本地 sheet_id.txt。

    刻意**不在代码里写死 ID**：仓库要公开，写死等于把表格 ID 挂在公网。
    sheet_id.txt 已在 .gitignore 里，只存在于本机。
    换表时改 sheet_id.txt 一行即可，或在 CI 里配 Secret SHEET_ID。
    """
    v = os.environ.get("SHEET_ID", "").strip()
    if v:
        return v
    try:
        with open(SHEET_ID_FILE, encoding="utf-8") as f:
            v = f.read().strip()
    except OSError:
        v = ""
    return v


def require_sheet_id():
    """取表格 ID；取不到就报错退出（避免拿空字符串去调 open_by_key）。"""
    sid = resolve_sheet_id()
    if not sid:
        raise SystemExit(
            "\n❌ 没找到 Google 表格 ID。二选一：\n"
            f"  1) 在本机建文件 {SHEET_ID_FILE}，里面只写一行表格 ID\n"
            "  2) 设环境变量 SHEET_ID（CI 走仓库 Settings → Secrets → SHEET_ID）\n"
            "  （表格 ID = 浏览器地址栏 /spreadsheets/d/<这一串>/edit 中间那串）\n")
    return sid


# 全项目只有本文件这一处权威定义，push_to_gsheet.py / build_herb_sheets.py 都从这里取值。
SHEET_ID = resolve_sheet_id()
SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", os.path.join(BASE_DIR, "service_account.json"))
LOCAL_PROXY = os.environ.get("LOCAL_PROXY", "http://127.0.0.1:3213")
# 常见本地代理端口，直连不通时逐个尝试（你只需开着代理，不用管具体端口）
# 覆盖 Clash(7890)、v2ray(1080/10809)、ShadowSocks(1080/1087)、通用 HTTP(8080) 等
PROXY_CANDIDATES = [
    LOCAL_PROXY,
    "http://127.0.0.1:3213",
    "http://127.0.0.1:7890",
    "http://127.0.0.1:1080",
    "http://127.0.0.1:1087",
    "http://127.0.0.1:10809",
    "http://127.0.0.1:8080",
]
GSPREAD_TIMEOUT = int(os.environ.get("GSPREAD_TIMEOUT", "30"))


def build():
    """读取 JSON，构建三张表的数据。返回 dict。"""
    with open(DATA_JSON, "r", encoding="utf-8") as f:
        raw = json.load(f)

    # 稳定排序：品名, 规格, 产地
    combos = sorted(raw.keys())
    daily_rows = []          # [品名,规格,产地,日期,价格]
    index_rows = []          # [品名,规格,产地,起始行,结束行]
    monthly = {}             # combo -> {YYYY-MM: [(day,price), ...]}
    cur = 2                  # history_daily 第 1 行是表头，数据从第 2 行起

    for key in combos:
        name, spec, origin = key.split("|", 2)
        pts = raw[key]
        n = len(pts)
        if n == 0:
            continue
        start = cur
        for date, price in pts:
            daily_rows.append([name, spec, origin, date, price])
            if not date or len(date) < 10:
                continue
            ym = date[:7]
            try:
                day = int(date[8:10])
                monthly.setdefault(key, defaultdict(list)).setdefault(ym, []).append((day, float(price)))
            except (TypeError, ValueError):
                pass
        end = cur + n - 1
        # 首列存拼接键，供 medicine_data 的链接 VLOOKUP 精确匹配
        index_rows.append([key, spec, origin, start, end])
        cur += n

    # 月度列头（所有 combo 出现的月份并集，升序）+ 末尾「趋势」迷你折线列
    all_months = set()
    for m in monthly.values():
        all_months.update(m.keys())
    all_months = sorted(all_months)
    monthly_header = ["品名", "规格", "产地"] + all_months + ["趋势"]
    monthly_rows = []
    for key in combos:
        name, spec, origin = key.split("|", 2)
        m = monthly.get(key, {})
        row = [name, spec, origin]
        for ym in all_months:
            vals = m.get(ym)
            row.append(_month_val(vals) if vals else "")
        monthly_rows.append(row)

    stats = {
        "combos": len(combos),
        "daily_points": len(daily_rows),
        "months": len(all_months),
        "month_span": f"{all_months[0]} ~ {all_months[-1]}" if all_months else "-",
    }
    return {
        "daily_header": ["品名", "规格", "产地", "日期", "价格"],
        "daily_rows": daily_rows,
        "index_header": ["品名", "规格", "产地", "起始行", "结束行"],
        "index_rows": index_rows,
        "monthly_header": monthly_header,
        "monthly_rows": monthly_rows,
        "stats": stats,
    }


def _sparkline_formula(row_i):
    """（已弃用，保留兼容）"""
    lookup = f'A{row_i}&"|"&B{row_i}&"|"&C{row_i}'
    return (f'=IFERROR(SPARKLINE(INDEX(history_daily!$E:$E,'
            f'VLOOKUP({lookup},history_index!$A:$E,4,FALSE)):'
            f'INDEX(history_daily!$E:$E,'
            f'VLOOKUP({lookup},history_index!$A:$E,5,FALSE)),'
            f'{{"charttype","line";"linewidth",1}}),"")')


# 历史价格网页地址（仅当用 --web-url 显式指定时才走网页跳转；否则默认在表格内跳转）
# 优先级：命令行 --web-url  >  环境变量 HISTORY_WEB_URL  >  web_url.txt  >  空（表格内跳转）
WEB_BASE_DEFAULT = ""


def load_web_base():
    """读取网页地址（web_url.txt 里第一个不以 # 开头的非空行）。无则返回空字符串（表格内跳转）。"""
    p = os.path.join(BASE_DIR, "web_url.txt")
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if s and not s.startswith("#"):
                        return s.rstrip("#").strip()
        except Exception:
            pass
    return os.environ.get("HISTORY_WEB_URL", WEB_BASE_DEFAULT)


def _link_web_formula(row_i, base):
    """medicine_data 第 row_i 行的「历史价格」列：点一下跳到该品种的历史价格网页。"""
    return (f'=HYPERLINK("{base}#"&ENCODEURL(A{row_i}&"|"&B{row_i}&"|"&C{row_i}),'
            f'"查看历史价格")')


def _link_sheet_formula(row_i, gid, target_row):
    """medicine_data 第 row_i 行：表格内跳转到 history_monthly 的 target_row 行。"""
    return f'=HYPERLINK("#gid={gid}&range=A{target_row}","查看历史价格")'


def push_to_gsheet(built, web_base, skip_daily=False):
    old = socket.getdefaulttimeout()
    socket.setdefaulttimeout(GSPREAD_TIMEOUT)
    try:
        creds = Credentials.from_service_account_file(
            SERVICE_ACCOUNT_FILE,
            scopes=["https://www.googleapis.com/auth/spreadsheets"])
        client = gspread.authorize(creds)
        sh = client.open_by_key(SHEET_ID)
    finally:
        socket.setdefaulttimeout(old)

    # ---- 只写链接列（--links-only 快通道）----
    if built is None:
        write_link_column(sh, web_base)
        return

    # ---- history_daily（分批写，避免单次请求体过大）----
    # skip_daily：日级数据改由 build_herb_sheets.py 的 daily_pool 承载（更省格子），
    #             这里就不再写 100 万行、也不再建 history_index。
    if skip_daily:
        print("⏭ 已跳过 history_daily / history_index（--no-daily）")
    else:
        dh, dr = built["daily_header"], built["daily_rows"]
        try:
            ws = sh.worksheet("history_daily")
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="history_daily",
                                  rows=len(dr) + 10, cols=len(dh))
        ws.append_row(dh)
        step = 20000
        for i in range(0, len(dr), step):
            ws.append_rows(dr[i:i + step])
            print(f"  history_daily 已写 {min(i+step, len(dr))}/{len(dr)}")
        print(f"✅ history_daily：{len(dr)} 行")

        # ---- history_index ----
        ih, ir = built["index_header"], built["index_rows"]
        try:
            wi = sh.worksheet("history_index")
            wi.clear()
        except gspread.WorksheetNotFound:
            wi = sh.add_worksheet(title="history_index", rows=len(ir) + 10, cols=len(ih))
        wi.update([ih] + ir, "A1", value_input_option="RAW")
        print(f"✅ history_index：{len(ir)} 行")

    # ---- history_monthly ----
    mh, mr = built["monthly_header"], built["monthly_rows"]
    n_months = built["stats"]["months"]
    try:
        wm = sh.worksheet("history_monthly")
        wm.clear()
    except gspread.WorksheetNotFound:
        wm = sh.add_worksheet(title="history_monthly", rows=len(mr) + 10, cols=len(mh))
    wm.update([mh] + mr, "A1", value_input_option="RAW")
    # 末尾「趋势」列：同一行内画迷你折线（引用本行月份单元格，渲染稳定）
    first_mcol = 4                                  # D 列起为月份
    last_mcol = 3 + n_months                        # 最后一列月份
    trend_col = last_mcol + 1                       # 趋势列
    spark = []
    for i in range(2, len(mr) + 2):
        rng = f"{col_letter(first_mcol)}{i}:{col_letter(last_mcol)}{i}"
        spark.append([f'=IFERROR(SPARKLINE({rng}),"")'])
    wm.update(f"{col_letter(trend_col)}2:{col_letter(trend_col)}{len(mr)+1}",
              spark, value_input_option="USER_ENTERED")
    print(f"✅ history_monthly：{len(mr)} 行 × {len(mh)} 列（含趋势迷你图）")

    # ---- medicine_data：写「历史价格」链接列（放在「更新日期」前面，更新日期保持最后）----
    write_link_column(sh, web_base)


def write_link_column(sh, web_base):
    """在 medicine_data 写入/刷新「历史价格」链接列。
    默认在表格内跳转（跳到 history_monthly 对应行）；仅当 web_base 是 http(s) 链接时才跳网页。
    """
    wd = sh.worksheet("medicine_data")
    all_vals = wd.get_all_values()
    if not all_vals:
        print("⚠️ medicine_data 是空的，跳过")
        return
    header = all_vals[0]
    n = len(all_vals)
    if "历史价格" in header:
        hi_col = header.index("历史价格") + 1          # 已存在：只重写这一列
    else:
        if "更新日期" not in header:
            print("⚠️ medicine_data 找不到「更新日期」列，无法定位插入位置，跳过历史价格列")
            return
        date_col = header.index("更新日期") + 1        # 1-based
        hi_col = date_col                             # 历史价格插在更新日期前面
        # 把「更新日期」整列右移一格（写到 date_col+1），腾出当前列给历史价格
        date_vals = [[row[date_col - 1]] if len(row) > date_col - 1 else [""]
                     for row in all_vals]
        tgt = col_letter(date_col + 1)
        wd.update(f"{tgt}1:{tgt}{n}", date_vals, value_input_option="RAW")
        wd.update_cell(1, hi_col, "历史价格")

    # 是否走网页跳转
    use_web = bool(web_base) and web_base.lower().startswith(("http://", "https://"))

    # 预备：history_monthly 的 品名|规格|产地 -> 行号 映射（用于表格内跳转）
    row_map = {}
    gid = None
    if not use_web:
        try:
            wm = sh.worksheet("history_monthly")
            gid = wm.id
            mvals = wm.get_all_values()
            for idx, r in enumerate(mvals[1:], start=2):   # 数据从第 2 行起
                if len(r) >= 3:
                    row_map[f"{r[0]}|{r[1]}|{r[2]}"] = idx
        except gspread.WorksheetNotFound:
            print("⚠️ 未找到 history_monthly，回退为网页跳转（如未配置网页则链接为空）")
            use_web = bool(web_base) and web_base.lower().startswith(("http://", "https://"))

    formulas = []
    for i in range(2, n + 1):
        row = all_vals[i - 1]
        if len(row) >= 3:
            key = f"{row[0]}|{row[1]}|{row[2]}"
        else:
            key = ""
        if use_web:
            formulas.append([_link_web_formula(i, web_base)])
        else:
            tgt_row = row_map.get(key, 1)
            formulas.append([_link_sheet_formula(i, gid, tgt_row)])
    col = col_letter(hi_col)
    wd.update(f"{col}2:{col}{n}", formulas, value_input_option="USER_ENTERED")
    if use_web:
        print(f"✅ medicine_data 已写「历史价格」网页链接列（第 {hi_col} 列，{n-1} 行，跳转 {web_base}）")
    else:
        print(f"✅ medicine_data 已写「历史价格」表格内跳转列（第 {hi_col} 列，{n-1} 行；"
              f"点击跳到 history_monthly 对应行；更新日期在第 {hi_col + 1} 列 = 最后）")


def _run_with_proxy(fn):
    """先直连；超时/解析失败则依次尝试常见本地代理端口（开着代理即可，不用管端口是几）。"""
    try:
        fn()
        return True
    except Exception as e:
        err = str(e)
        print(f"⚠️ 直连失败：{err[:160]}")
        # 只有「网络不通」类错误才值得换代理；其他错误（如表格/权限问题）直接抛出
        if not ("timeout" in err.lower() or "timed out" in err.lower()
                or "Name or service" in err or "Failed to resolve" in err
                or "Connection refused" in err or "Network is unreachable" in err):
            raise

    tried = []
    for proxy in PROXY_CANDIDATES:
        if not proxy or proxy in tried:
            continue
        tried.append(proxy)
        print(f"尝试代理 {proxy} ...")
        os.environ["HTTP_PROXY"] = proxy
        os.environ["HTTPS_PROXY"] = proxy
        try:
            fn()
            print(f"✅ 已通过 {proxy} 连上 Google")
            return True
        except Exception as e2:
            print(f"   {proxy} 失败：{str(e2)[:120]}")
    print("❌ 所有代理端口都失败。请确认：代理软件已开启，且它能上 Google。")
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="只本地构建并打印统计，不连接 Google Sheet")
    ap.add_argument("--links-only", action="store_true",
                    help="只刷新 medicine_data 的「历史价格」链接列（很快，不重传历史数据）")
    ap.add_argument("--web-url", default=None,
                    help="历史价格网页地址，如 https://xxx.pages.dev/index.html")
    ap.add_argument("--no-daily", action="store_true",
                    help="不写 history_daily/history_index（日数据交给 build_herb_sheets 的 daily_pool）")
    args = ap.parse_args()

    web_base = (args.web_url or load_web_base()).rstrip("#").strip()
    if web_base and web_base.lower().startswith(("http://", "https://")):
        print(f"「历史价格」列跳转方式：网页（{web_base}）")
    else:
        print("「历史价格」列跳转方式：表格内跳转（history_monthly 对应行）")

    if args.links_only:
        print("只刷新「历史价格」链接列（不重传历史数据）...")
        ok = _run_with_proxy(lambda: push_to_gsheet(None, web_base))
        if ok:
            print("\n🎉 链接列已刷新。")
        return

    print("读取并构建历史数据 ...")
    built = build()
    s = built["stats"]
    print(f"  组合数    : {s['combos']}")
    print(f"  日价格点  : {s['daily_points']:,}")
    print(f"  月度跨度  : {s['month_span']}（{s['months']} 个月）")

    if args.dry_run:
        print("\n[dry-run] 未连接 Google Sheet。数据构建逻辑已验证。")
        return

    ok = _run_with_proxy(lambda: push_to_gsheet(built, web_base, skip_daily=args.no_daily))
    if ok:
        print("\n🎉 全部写入完成。")


if __name__ == "__main__":
    main()
