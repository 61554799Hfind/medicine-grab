@echo off
chcp 65001 >nul
cd /d D:\medicine_grab

echo ============================================
echo   药材涨落数据 - 快速抓取
echo ============================================
echo.
echo  1 = 试跑前 20 味（首次使用建议先跑这个）
echo  2 = 正式全量抓取（约 16 分钟，含别名补全）
echo  3 = 断点续跑（上次中断后用）
echo  4 = 保守模式（更慢更稳）
echo  5 = 退回旧版浏览器方式（最稳但最慢，约 70 分钟）
echo.
echo  注意：跑之前请先关闭已打开的 Edge 窗口。
echo.
set /p CHOICE=请输入序号后回车:

if "%CHOICE%"=="1" goto T1
if "%CHOICE%"=="2" goto T2
if "%CHOICE%"=="3" goto T3
if "%CHOICE%"=="4" goto T4
if "%CHOICE%"=="5" goto T5
goto T1

:T1
echo.
echo [试跑 20 味]
D:\medicine_grab\.venv\Scripts\python.exe fast_crawl.py --limit 20 --no-push
goto END

:T2
echo.
echo [全量抓取（含别名补全）]
D:\medicine_grab\.venv\Scripts\python.exe fast_crawl.py --alias
goto END

:T3
echo.
echo [断点续跑]
D:\medicine_grab\.venv\Scripts\python.exe fast_crawl.py --resume
goto END

:T4
echo.
echo [保守模式]
D:\medicine_grab\.venv\Scripts\python.exe fast_crawl.py --slow
goto END

:T5
echo.
echo [旧版浏览器方式]
D:\medicine_grab\.venv\Scripts\python.exe search_crawl_slim.py
goto END

:END
echo.
echo 完成。日志在 crawl.log
pause
