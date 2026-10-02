from playwright.sync_api import sync_playwright
import json, random, time

EDGE_PROFILE = r"D:\medicine_grab\edge_profile"
EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
BASE_URL = "https://www.yt1998.com/price/nowDayPriceQ!getZhangLuo.do"

# 周期参数: paramName / paramValue
PERIODS = [
    ("日",   "yesterdayClick", 1),
    ("周",   "zhouduibiClick", 1),
    ("月",   "yueduibiClick", 1),
    ("季度", "jiduibiClick",   1),
    ("年",   "nianduibiClick", 1),
]

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=EDGE_PROFILE,
        executable_path=EDGE_EXE,
        headless=True,
        args=["--no-proxy-server"],
    )
    page = ctx.new_page()
    # 先访问页面，种下 cookie / session
    page.goto("https://www.yt1998.com/priceUpDown.html", wait_until="domcontentloaded")
    page.wait_for_timeout(3000)

    for period_name, pname, pval in PERIODS:
        params = {
            "random": random.random(),
            "ascFlag": "zhang",
            "ycnam": "麻黄",
            "market": "1",
            "leibie": "",
            "istoday": "0",
            "spices": "",
            "tea": "",
            "paramName": pname,
            "paramValue": pval,
            "pageIndex": 0,
            "pageSize": 20,
        }
        url = BASE_URL + "?" + "&".join(f"{k}={v}" for k, v in params.items())
        resp = page.request.get(url)
        print(f"\n===== 周期【{period_name}】状态码 {resp.status} =====")
        print(resp.text()[:2000])
        page.wait_for_timeout(800)

    ctx.close()
