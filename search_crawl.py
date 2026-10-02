from playwright.sync_api import sync_playwright
import time
import csv
import os
import random
from datetime import datetime

# =========配置=========
MEDICINE_FILE = "medicine_list.txt"
OUTPUT_CSV = "药材涨落对比结果.csv"
FAILED_TXT = "failed_medicines.txt"
TARGET_URL = "https://www.yt1998.com/priceUpDown.html"

EDGE_PROFILE = r"D:\medicine_grab\edge_profile"
EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROXY_URL = "http://127.0.0.1:3213"
# ======================

# 读取txt药材列表
with open(MEDICINE_FILE, "r", encoding="utf-8") as f:
    medicine_names = [line.strip() for line in f if line.strip()]

header = ["品名", "规格", "产地", "原价", "现价(元/kg)", "走势",
          "昨日对比", "周对比", "月对比", "季度对比", "年对比", "更新日期"]
today = datetime.now().strftime("%Y-%m-%d")

def wait_for_table_load(page, timeout=20000, context_name="数据"):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            if page.locator("text=共0条").count() > 0:
                print(f"  [提示] 页面显示“共0条”，{context_name}为空。")
                return False
            rows = page.locator("table.tab11 tbody#priceList tr").all()
            if len(rows) > 0:
                first_td = rows[0].locator("td").first
                if first_td.count() > 0:
                    text = first_td.inner_text().strip()
                    if text and text not in ["暂无数据", "没有数据", "无"]:
                        return True
        except Exception:
            pass
        page.wait_for_timeout(500)
    return False

def extract_table_data(page):
    data_rows = []
    try:
        rows = page.locator("table.tab11 tbody#priceList tr").all()
        if not rows:
            return data_rows
        for tr in rows:
            tds = tr.locator("td").all()
            if len(tds) < 7:
                continue
            spec = tds[1].inner_text().strip()
            origin = tds[2].inner_text().strip()
            old_price = tds[3].inner_text().strip()
            new_price = tds[4].inner_text().strip()
            trend = tds[5].inner_text().strip()
            change = tds[6].inner_text().strip()
            if not spec and not origin:
                continue
            data_rows.append({
                "规格": spec,
                "产地": origin,
                "原价": old_price,
                "现价(元/kg)": new_price,
                "走势": trend,
                "涨跌幅": change
            })
    except Exception as e:
        print(f"  [警告] 提取表格数据出错: {e}")
    return data_rows

def run():
    all_data = []
    failed_medicines = []
    target_medicines = medicine_names  # 测试取前10，正式改为 medicine_names

    # =========阶段1：脚本内部拉起持久化Edge，不走VPN代理=========
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=EDGE_PROFILE,
            executable_path=EDGE_EXE,
            headless=False,
            args=["--no-proxy-server"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
            viewport={"width":1920, "height":1080},
        )
        page = context.new_page()

        for i, med_name in enumerate(target_medicines, 1):
            print(f"\n==== [{i}/{len(target_medicines)}] 正在搜索：{med_name} ====")
            try:
                goto_ok = False
                for retry_times in range(1):
                    try:
                        page.goto(TARGET_URL, timeout=60000, wait_until="domcontentloaded")
                        goto_ok = True
                        break
                    except Exception as e:
                        print(f"  ⚠️页面访问失败，重试 {retry_times+1}：{str(e)[:120]}")
                        page.wait_for_timeout(4000)
                if not goto_ok:
                    raise Exception("多次访问页面被拒绝")

                page.wait_for_timeout(random.randint(3000, 4500))
                page.add_style_tag(content="""
                    .ad, .advertisement, [id^='ad'], [class^='ad'],
                    .float-ad, #floatAd, .popup {
                        display: none !important;
                        visibility: hidden !important;
                        pointer-events: none !important;
                    }
                """)
                page.locator("input#kw").clear()
                page.wait_for_timeout(random.randint(500,1000))
                page.locator("input#kw").fill(med_name)
                page.wait_for_timeout(random.randint(500, 1000))
                page.locator("input#su").click()

                print("  [信息] 开始按顺序点击筛选（类别全部 -> 涨落全部）...")
                page.wait_for_timeout(1000)
                try:
                    cat_all = page.locator("a[onclick*=\"toSearch('leibie','')\"]").first
                    if cat_all.count()>0:
                        cat_all.wait_for(state="visible", timeout=3000)
                        cat_all.click(force=True)
                        page.wait_for_timeout(random.randint(800, 1200))
                except Exception as e:
                    print(f"  [警告] 点击『类别‑全部』失败(可忽略): {e}")

                try:
                    trend_all = page.locator("a[onclick*=\"toSearch('ascFlag','')\"]").first
                    if trend_all.count()>0:
                        trend_all.wait_for(state="visible", timeout=3000)
                        trend_all.click(force=True)
                        page.wait_for_timeout(random.randint(800, 1200))
                except Exception as e:
                    print(f"  [警告] 点击『涨落‑全部』失败(可忽略): {e}")

                periods = [
                    ("日", "yesterdayClick", "昨日对比"),
                    ("周", "zhouduibiClick", "周对比"),
                    ("月", "yueduibiClick", "月对比"),
                    ("季度", "jiduibiClick", "季度对比"),
                    ("年", "nianduibiClick", "年对比")
                ]
                base_rows = []
                got_any_data = False
                for period_name, period_id, dict_key in periods:
                    print(f"  [信息] 正在尝试抓取【{period_name}】的数据...")
                    try:
                        btn = page.locator(f"a#{period_id}")
                        if btn.count() > 0:
                            btn.wait_for(state="visible", timeout=5000)
                            btn.click(force=True)
                            page.wait_for_timeout(random.randint(800, 1200))
                    except Exception as e:
                        print(f"  [警告] 点击【{period_name}】失败: {e}")
                        continue

                    if wait_for_table_load(page, timeout=20000, context_name=f"{period_name}数据"):
                        rows = extract_table_data(page)
                        if rows:
                            got_any_data = True
                            if not base_rows:
                                base_rows = rows
                                print(f"  ✅ 已建立基础数据（来源：{period_name}），共 {len(rows)} 条")
                            else:
                                time_dict = {f"{r['规格']}_{r['产地']}": r['涨跌幅'] for r in rows}
                                for base_row in base_rows:
                                    key = f"{base_row['规格']}_{base_row['产地']}"
                                    base_row[dict_key] = time_dict.get(key, "")
                                print(f"  ✅ {period_name} 数据已合并，共 {len(rows)} 条")
                        else:
                            print(f"  ⚠️ {period_name} 数据解析为空")
                    else:
                        print(f"  ⚠️ {period_name} 等待超时或无数据，继续尝试下一个周期...")

                if not got_any_data:
                    print(f"  ❌ 【{med_name}】所有周期（日/周/月/季/年）均无数据，记录到失败列表。")
                    failed_medicines.append(f"{med_name} (所有周期无数据)")
                    continue

                for row in base_rows:
                    all_data.append([
                        med_name, row.get("规格", ""), row.get("产地", ""), row.get("原价", ""),
                        row.get("现价(元/kg)", ""), row.get("走势", ""), row.get("涨跌幅", ""),
                        row.get("周对比", ""), row.get("月对比", ""), row.get("季度对比", ""),
                        row.get("年对比", ""), today
                    ])
                print(f"  ✅ {med_name} 数据整理完成")

            except Exception as e:
                print(f"抓取【{med_name}】出错：{e}")
                failed_medicines.append(f"{med_name} (异常: {str(e)[:20]})")
            finally:
                time.sleep(random.uniform(8,14))

        context.close()
        print("\n浏览器已关闭，准备推送Google Sheet")

    # =========阶段2：Python开启代理推送Google Sheet=========
    os.environ["HTTP_PROXY"] = PROXY_URL
    os.environ["HTTPS_PROXY"] = PROXY_URL
    try:
        from push_to_gsheet import push_data
        status = "正常" if not failed_medicines else f"部分失败({len(failed_medicines)}个)"
        push_data(header, all_data, failed_list=failed_medicines, status_msg=status)
        print("Google Sheet推送成功")
    except Exception as e:
        print(f"⚠️推送 Google Sheet 失败：{e}")
        print("本地CSV仍然保存成功")
    finally:
        if "HTTP_PROXY" in os.environ:
            del os.environ["HTTP_PROXY"]
        if "HTTPS_PROXY" in os.environ:
            del os.environ["HTTPS_PROXY"]

    # 写入本地CSV
    with open(OUTPUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(all_data)
    print(f"\n抓取完成！结果保存到 {OUTPUT_CSV}")
    print(f"   共 {len(all_data)} 条记录，抓取日期：{today}")
    if failed_medicines:
        with open(FAILED_TXT, "w", encoding="utf-8") as f:
            f.write("以下药材未能获取到数据：\n")
            for name in failed_medicines:
                f.write(name + "\n")
        print(f"共有 {len(failed_medicines)} 个药材未获取到数据，名单已保存到 {FAILED_TXT}")
    else:
        print("所有药材都成功获取到了数据！")

if __name__ == "__main__":
    run()
