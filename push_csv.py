# -*- coding: utf-8 -*-
r"""
把「药材涨落对比结果.csv」直接推送到 Google Sheet 的 medicine_data 表。

为什么需要它：
  本机有代理冲突 —— 开代理能上 Google 但访问不了药通网，关代理反过来。
  所以抓取（fast_crawl.py，要关代理）和推送（本脚本，要开代理）必须分开跑。
  抓取时用 `fast_crawl.py --no-push` 只落地 CSV，再开着代理跑本脚本把 CSV 推上去。

用法：
    .venv\Scripts\python.exe push_csv.py

「历史价格」列（第 12 列）由 build_herb_sheets.py 统一写入表格内跳转，本脚本留空。
"""
import csv
import os

from push_to_gsheet import push_data_auto

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "药材涨落对比结果.csv")

HEADER = ["品名", "规格", "产地", "原价", "现价(元/kg)", "走势",
          "昨日对比", "周对比", "月对比", "季度对比", "年对比", "历史价格", "更新日期"]


def load_csv():
    with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise SystemExit(f"{CSV_FILE} 是空的")
    out = []
    for r in rows[1:]:
        if len(r) < 11 or not r[0].strip():
            continue
        update = r[11].strip() if len(r) > 11 else ""
        # 前 11 列 + 历史价格(空) + 更新日期  → 共 13 列
        out.append([c.strip() for c in r[:11]] + ["", update])
    return out


def main():
    if not os.path.exists(CSV_FILE):
        raise SystemExit(f"找不到 {CSV_FILE}，请先跑 fast_crawl.py")
    rows = load_csv()
    print(f"读取 {CSV_FILE}：{len(rows)} 行")
    if not rows:
        raise SystemExit("CSV 里没有有效数据行")
    ok = push_data_auto(HEADER, rows)
    if not ok:
        raise SystemExit(1)
    print("\n✅ medicine_data 已更新。接着跑 build_herb_sheets.py 画大图并刷新「历史价格」链接。")


if __name__ == "__main__":
    main()
