# push_to_gsheet.py
import os
import socket

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

# 网络超时（秒）。国内直连 Google 会卡住不返回，设了超时才能快速失败并回退到代理。
GSPREAD_TIMEOUT = int(os.environ.get("GSPREAD_TIMEOUT", "45"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_BASE = os.path.dirname(os.path.abspath(__file__))
# 服务端可通过环境变量指定密钥路径（CI 里从 secrets 还原出来的临时文件）
SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", os.path.join(_BASE, "service_account.json"))
# 目标表格ID：以 push_history.py 为唯一来源（那里也支持环境变量 SHEET_ID 覆盖）。
# 想换表时不用改代码：设环境变量 SHEET_ID 即可，例如
#   Windows:  set SHEET_ID=1FwyR-...
#   CI:       仓库 Settings → Secrets → 新增 SHEET_ID
try:
    from push_history import SHEET_ID, resolve_sheet_id, require_sheet_id
except Exception:  # 单独使用本文件时兜底
    SHEET_ID = os.environ.get("SHEET_ID", "").strip()
    resolve_sheet_id = None
    require_sheet_id = None
# 注意：这里刻意不写死表格 ID（仓库要公开）。
# 取不到就留空，真正使用前由 require_sheet_id() 给出明确报错。

# 「历史价格」列（第 12 列 / L）不放链接，留空由 build_herb_sheets.py 统一写入表格内跳转。
# 原因：以前这里会写死一个 127.0.0.1 的本机网页地址，老板点开必然打不开。


def push_data(header, rows, failed_list=None, status_msg=""):
    # ⚠️ 这里以前写成 `require_sheet_id() if resolve_sheet_id else ...`，
    # 但上面只 import 了 resolve_sheet_id，导致 NameError 被上层 except 吃掉，
    # 表现成"C I 里推送失败但只说一句推送失败"，很难查。取值统一走这条链。
    if require_sheet_id is not None:
        sid = require_sheet_id()
    else:
        sid = (SHEET_ID or os.environ.get("SHEET_ID", "")).strip()
    if not sid:
        raise RuntimeError("没找到表格 ID：请建 sheet_id.txt 或设环境变量 SHEET_ID")
    old_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(GSPREAD_TIMEOUT)
    try:
        creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
        client = gspread.authorize(creds)
        try:
            sh = client.open_by_key(sid)
        except gspread.SpreadsheetNotFound:
            raise RuntimeError(
                f"找不到表格 {sid}。请确认已把表格「共享」给服务账号 "
                f"{creds.service_account_email}（权限设为编辑者）")
        except gspread.exceptions.APIError as e:
            raise RuntimeError(f"Google API 拒绝了请求（多半是没共享表格或 API 未启用）：{e}")

        # ========== 1. 写入成功的数据到 medicine_data ==========
        try:
            ws = sh.worksheet("medicine_data")
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title="medicine_data",
                                  rows=len(rows) + 10, cols=len(header) + 2)

        # 分三块写入（跳过历史价格列里的公式由本函数一并写入，保证每天重跑不会把它冲掉）：
        #   A1:K  → 品名~年对比（前 11 列，含表头）
        #   L1:L  → 历史价格（超链接，第 12 列）
        #   M1:M  → 更新日期（第 13 列，最后）
        left = [header[:11]] + [[str(c) for c in row[:11]] for row in rows]
        mid_header = header[11] if len(header) > 11 else "历史价格"
        # 历史价格列只写表头，单元格留空（跳转链接由 build_herb_sheets.py 统一刷新）
        links = [[mid_header]] + [[""] for i in range(2, len(rows) + 2)]
        right = [["更新日期"]] + [[str(row[12]) if len(row) > 12 else ""] for row in rows]
        ws.clear()
        ws.update(left, "A1", value_input_option="RAW")
        ws.update(links, "L1", value_input_option="USER_ENTERED")
        ws.update(right, "M1", value_input_option="RAW")
        print(f"成功数据已推送到 [medicine_data]：{len(rows)} 行（含「历史价格」链接列）")

        # ========== 2. 写入失败名单到 none ==========
        try:
            ws_none = sh.worksheet("none")
        except gspread.WorksheetNotFound:
            ws_none = sh.add_worksheet(title="none", rows=100, cols=2)

        ws_none.clear()
        if failed_list:
            ws_none.update([[name] for name in failed_list], "A1", value_input_option="RAW")
            print(f"失败数据已推送到 [none]：{len(failed_list)} 条")
        else:
            ws_none.update([["本次运行无失败数据"]], "A1", value_input_option="RAW")
            print("本次运行无失败数据，[none] 表已清空")
    finally:
        socket.setdefaulttimeout(old_timeout)

    # ========== 3. 把最后更新时间写到第一个sheet的角落 ==========
    # try:
    #     main = sh.sheet1
    #     ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    #     main.update([["最后更新", ts], ["状态", status_msg]], "A1")
    # except Exception as e:
    #     print(f"[提示] 写状态失败（可忽略）：{e}")


# 常见本地代理端口：直连不通时逐个尝试（你只需开着代理，不用管具体端口）
PROXY_CANDIDATES = [
    os.environ.get("LOCAL_PROXY", "http://127.0.0.1:3213"),
    "http://127.0.0.1:3213", "http://127.0.0.1:7890", "http://127.0.0.1:1080",
    "http://127.0.0.1:1087", "http://127.0.0.1:10809", "http://127.0.0.1:8080",
]


def _is_net_err(e):
    s = str(e).lower()
    return any(k in s for k in ("timeout", "timed out", "failed to resolve",
                                "name or service", "connection refused",
                                "network is unreachable", "tunnel connection failed",
                                "max retries exceeded", "proxyerror"))


def push_data_auto(header, rows, failed_list=None, status_msg=""):
    """先直连；网络不通时依次尝试常见本地代理端口。成功返回 True。

    本机场景必须用它：直连 Google 不通，需要走代理；而抓药通网又要关代理，两者互斥，
    所以抓取（--no-push）和推送分成两步跑。
    """
    try:
        push_data(header, rows, failed_list, status_msg)
        return True
    except Exception as e:
        if not _is_net_err(e):
            raise
        print(f"⚠️ 直连 Google 失败：{str(e)[:160]}")

    tried = []
    for p in PROXY_CANDIDATES:
        if not p or p in tried:
            continue
        tried.append(p)
        print(f"尝试代理 {p} ...")
        os.environ["HTTP_PROXY"] = p
        os.environ["HTTPS_PROXY"] = p
        try:
            push_data(header, rows, failed_list, status_msg)
            print(f"✅ 已通过 {p} 连上 Google")
            return True
        except Exception as e2:
            print(f"   {p} 失败：{str(e2)[:120]}")
    print("❌ 所有代理端口都失败。请确认代理软件已开启且能上 Google。")
    return False
    #     print(f"[提示] 写状态失败（可忽略）：{e}")