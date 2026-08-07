@echo off
REM ============================================================
REM OKX funding 机制采样探测器 - Windows 运行入口
REM
REM 与 run.bat / run_monitor.bat 的关键区别：
REM   这是【长跑前台进程】，不是定时任务。它会一直采样直到你 Ctrl+C 或关窗口。
REM   所以【不要】用 schtasks /sc hourly 那套，见下面「开机自启」。
REM
REM 【依赖】：python 3.9+ 且已装 requests
REM   pip install requests
REM   （只有 --report 分析模式才需要 pandas，采样本身不需要）
REM
REM 【PYTHONIOENCODING 不能删】：脚本输出含 emoji(⚠ ❌ 🌐 📈 🔔 🛑)。
REM   Windows 中文系统把输出重定向到日志文件时默认用 GBK，emoji 无法编码，
REM   会让脚本在第一行 print 就 UnicodeEncodeError 直接崩掉，一行日志都写不出来。
REM   （run_monitor.bat 里已有同样的实测记录）
REM
REM 【代理】：脚本每轮启动先试直连，不通才用 PROXY_URL。若本机能直连 OKX 就设为空：
REM   setx PROXY_URL ""
REM   要走代理： setx PROXY_URL "http://127.0.0.1:7890"
REM   设完要【重开一个新的 cmd 窗口】才生效。
REM
REM ---------- 用法 ----------
REM 【默认 3 标的】双击本文件，或 cmd 里执行： run_probe.bat
REM 【指定标的】  run_probe.bat --insts SNDK,SOXL,BTC,MU,TSLA
REM 【多标的保密度】标的多时加 --no-mark，每标的请求数从 3 降到 1：
REM               run_probe.bat --insts A,B,C,...,T --no-mark --interval 20
REM 【调并发】    run_probe.bat --workers 5      (默认 3；OKX 限频严格，别调太高)
REM 【分析】      run_probe.bat --report
REM               run_probe.bat --report --since 2026-08-07
REM
REM ---------- 电源设置：必须做，否则采样会断 ----------
REM 休眠/待机会中断采样，CSV 出现大段空洞。以【管理员】cmd 执行：
REM   powercfg /change standby-timeout-ac 0      REM 接电源时永不待机
REM   powercfg /change hibernate-timeout-ac 0    REM 接电源时永不休眠
REM   powercfg /change monitor-timeout-ac 10     REM 屏幕可以关，不影响进程
REM 笔记本还要在「电源选项 - 选择关闭盖子的功能」里把合盖动作设为【不采取任何操作】。
REM 查看当前设置： powercfg /query
REM
REM ---------- 开机自启（可选）----------
REM 长跑进程用「登录时触发」而不是 hourly：
REM   schtasks /create /tn "FundingProbe" /sc onlogon /rl highest ^
REM     /tr "\"D:\stock_perp_analyse\run_probe.bat\" --insts SNDK,SOXL,BTC"
REM 查看： schtasks /query /tn "FundingProbe" /v /fo list
REM 停止： schtasks /end /tn "FundingProbe"
REM 删除： schtasks /delete /tn "FundingProbe" /f
REM
REM 注意 schtasks 不会自动重启崩掉的进程。要真正的守护进程用 NSSM：
REM   nssm install FundingProbe "D:\stock_perp_analyse\run_probe.bat"
REM   nssm set FundingProbe AppExit Default Restart
REM
REM ---------- 数据健康检查 ----------
REM 采样密度直接决定运行均值结论的可信度，静默丢样本等于结论作废。
REM 脚本每 20 轮打一次成功率摘要，成功率 <95% 的标的会单独告警 —— 日志里搜：
REM   findstr /C:"成功率" funding_probe.log
REM   findstr /C:"取不到" funding_probe.log
REM 日志会一直追加，长期跑记得定期归档（CSV 约 213 字节/行，20 标的约 8.8MB/天）。
REM ============================================================
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

REM --report 是交互式分析，输出要直接给人看，不能重定向进日志；
REM 采样才是长跑后台，输出追加到日志。
echo %* | findstr /C:"--report" >nul
if %errorlevel%==0 (
    python funding_probe.py %*
) else (
    python funding_probe.py %* >> funding_probe.log 2>&1
)
