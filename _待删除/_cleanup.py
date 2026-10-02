# -*- coding: utf-8 -*-
"""把待清理文件集中移到 _待删除 文件夹（可随时还原，不做永久删除）。"""
import os
import shutil

BASE = r"D:\medicine_grab"
TRASH = os.path.join(BASE, "_待删除")

FILES = [
    "crawl.log", "crawl_progress_trial.json", "试跑结果.csv",
    "probe_api.py", "validate_api.py", "test_gsheet.py", "test_playwright.py",
    "auth_state.json", "tencent_auth.json", "crawl_progress.json",
    "药材涨落对比结果_备份_旧版.csv", "run_all.bat",
]
DIRS = ["_debug", "__pycache__", "browser_profile", ".idea"]

os.makedirs(TRASH, exist_ok=True)
res = []

for name in FILES + DIRS:
    src = os.path.join(BASE, name)
    if not os.path.exists(src):
        res.append("SKIP  %s（不存在）" % name)
        continue
    dst = os.path.join(TRASH, name)
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True) if os.path.isdir(dst) else os.remove(dst)
    try:
        shutil.move(src, dst)
        res.append("OK    %s → _待删除/" % name)
    except Exception as e:
        res.append("FAIL  %s（%s）" % (name, str(e)[:70]))

# 清理产生的临时文件
for tmp in ["_clean_result.txt"]:
    p = os.path.join(BASE, tmp)
    if os.path.exists(p):
        try:
            shutil.move(p, os.path.join(TRASH, tmp))
        except Exception:
            pass

with open(os.path.join(BASE, "_clean_result.txt"), "w", encoding="utf-8") as f:
    f.write("\n".join(res))
print("\n".join(res))
