from playwright.sync_api import sync_playwright

EDGE_PROFILE = r"D:\medicine_grab\edge_profile"
EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

with sync_playwright() as p:
    context = p.chromium.launch_persistent_context(
        user_data_dir=EDGE_PROFILE,
        executable_path=EDGE_EXE,
        headless=False,
        args=["--no-proxy-server"] # Edge强制不走代理，访问药通网
    )
    page = context.new_page()
    page.goto("https://www.yt1998.com/priceUpDown.html")
    print("👉请在弹出的Edge窗口，手动登录药通网。登录完成后，回到控制台按回车。")
    input("登录完成，按回车保存登录信息并关闭浏览器……")
    context.close()
print("✅登录信息已经保存到 edge_profile，以后运行爬虫不用再登录！")
