from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    print("启动浏览器...")
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()
    print("访问药通网首页...")
    page.goto("https://www.yt1998.com/", timeout=30000)
    print("✅ 首页访问成功，标题：", page.title())
    print("访问价格页...")
    page.goto("https://www.yt1998.com/priceUpDown.html", timeout=30000)
    print("✅ 价格页访问成功")
    input("按 Enter 关闭...")
    browser.close()