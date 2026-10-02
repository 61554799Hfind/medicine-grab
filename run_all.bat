@echo off
start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="D:\medicine_grab\edge_profile"
echo Edge正在启动，等待8秒……
timeout /t 5 /nobreak
cd /d D:\medicine_grab
D:\medicine_grab\.venv\Scripts\python.exe search_crawl.py
pause