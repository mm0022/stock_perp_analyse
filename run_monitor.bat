@echo off
REM ============================================================
REM 持仓 funding 监控 - Windows 运行入口
REM 每小时扫一次 Biyi 持仓币，当期单期 funding < -5bp 推 Slack 告警
REM
REM 【依赖】：python 3.9+ 且已装 requests、pandas
REM   pip install requests pandas
REM
REM 【首次使用前，设一次环境变量】(cmd 里执行，只需一次)：
REM   setx SLACK_WEBHOOK_URL "https://hooks.slack.com/services/你的/webhook"
REM   setx PROXY_URL "http://127.0.0.1:7890"
REM       ^ 这是【回退候选】：脚本每轮先试直连，直连不通才用它。
REM         若本机能直连交易所，设为空即可： setx PROXY_URL ""
REM   setx ALERT_FUNDING_BP "-5"      (可选，默认 -5)
REM   设完要【重开一个新的 cmd 窗口】才生效。
REM
REM 【手动跑一次】：双击本文件，或在 cmd 里执行  run_monitor.bat
REM 【只算不发 Slack】：run_monitor.bat --dry-run
REM
REM 【定时每小时一次】：cmd 里执行（把路径换成本文件的实际路径）：
REM   schtasks /create /tn "PosFundingMonitor" /sc hourly /st 00:05 /tr "\"D:\stock_perp_analyse\run_monitor.bat\""
REM       /st 00:05 = 从 00:05 起每小时跑，落在 05 分而不是整点，
REM       避开交易所结算瞬间(费率此刻正在翻页)
REM   查看： schtasks /query /tn "PosFundingMonitor" /v /fo list
REM   立即试跑： schtasks /run /tn "PosFundingMonitor"
REM   删除： schtasks /delete /tn "PosFundingMonitor" /f
REM
REM 【PYTHONIOENCODING 不能删】：脚本输出含 emoji，Windows 中文系统把输出重定向到
REM   日志文件时默认用 GBK，emoji 无法编码，会让脚本在第一行 print 就
REM   UnicodeEncodeError 直接崩掉（已实测：退出码 1，一行日志都写不出来）。
REM
REM 【前提检查】：Biyi 是内网服务，这台 Windows 机器必须能访问
REM   https://biyi.tky.laozi.pro 否则监控拿不到持仓。先用浏览器打开确认。
REM ============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python pos_funding_monitor.py %* >> pos_funding_monitor.log 2>&1
