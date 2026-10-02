# test_gsheet.py
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SERVICE_ACCOUNT_FILE = "service_account.json"
SHEET_ID = "1fpy0IiZM-RaXI_XB4VBQvvuD_Wrr6tVm6GrS2uNmSRU"

creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
client = gspread.authorize(creds)
sh = client.open_by_key(SHEET_ID)
print("✅ 成功连上表格：", sh.title)

ws = sh.sheet1
ws.update([["测试时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")],
           ["说明", "如果你看到这行，说明 Python 已经能写你的 Google Sheet 了"]], "A1")
print("✅ 写入成功，去 Google Sheet 里看看吧！")