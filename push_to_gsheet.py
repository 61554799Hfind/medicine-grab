# push_to_gsheet.py
import os

import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_BASE = os.path.dirname(os.path.abspath(__file__))
# 服务端可通过环境变量指定密钥路径（CI 里从 secrets 还原出来的临时文件）
SERVICE_ACCOUNT_FILE = os.environ.get(
    "SERVICE_ACCOUNT_FILE", os.path.join(_BASE, "service_account.json"))
# 从你浏览器地址栏直接提取的表格ID
SHEET_ID = "1fpy0IiZM-RaXI_XB4VBQvvuD_Wrr6tVm6GrS2uNmSRU"


def push_data(header, rows, failed_list=None, status_msg=""):
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)

    # ========== 1. 写入成功的数据到 medicine_data ==========
    try:
        ws = sh.worksheet("medicine_data")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="medicine_data", rows=len(rows) + 10, cols=len(header) + 2)

    values = [header] + [[str(c) for c in row] for row in rows]
    ws.clear()
    ws.update(values, "A1", value_input_option="RAW")
    print(f"成功数据已推送到 [medicine_data]：{len(rows)} 行")

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

    # ========== 3. 把最后更新时间写到第一个sheet的角落 ==========
    # try:
    #     main = sh.sheet1
    #     ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    #     main.update([["最后更新", ts], ["状态", status_msg]], "A1")
    # except Exception as e:
    #     print(f"[提示] 写状态失败（可忽略）：{e}")