@echo off
REM ============================================================
REM 股票永续 funding 分析 - Windows 运行入口
REM
REM 【首次使用前，先设一次环境变量】(打开 cmd 执行，只需一次)：
REM   setx SLACK_WEBHOOK_URL "https://hooks.slack.com/services/你的/webhook"
REM   setx PROXY_URL "http://127.0.0.1:7890"
REM   (Windows 若不需要代理、能直连交易所，则设为空： setx PROXY_URL "" )
REM   设完要【重开一个新的 cmd 窗口】才生效。
REM
REM 【手动跑一次】：双击本文件，或在 cmd 里执行  run.bat
REM 【定时】：用任务计划程序(schtasks)指向本文件，见 README/说明
REM ============================================================
cd /d "%~dp0"
python stock_perp_24hvlum_openclaw.py >> run.log 2>&1
