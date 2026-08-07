"""控制台/日志输出的跨平台编码处理。**只用标准库**，任何脚本都能安全 import。

为什么单独一个模块：三个入口脚本(funding_probe / pos_funding_monitor /
stock_perp_24hvlum_openclaw)都需要同一套处理，复制三份必然分叉。放进
stock_perp_24hvlum_openclaw 会让 funding_probe 为此把 pandas 拉进采样路径，
也破坏探测器不依赖主脚本的独立性——所以独立成零依赖模块。

解决的问题：Windows 中文系统把输出重定向到文件时用 GBK，脚本里的 emoji
(⚠ ❌ 🌐 📈 🔔 🛑 ✅ 💾 …)编不出来 → UnicodeEncodeError 崩在第一行 print，
一行日志都写不出来（已实测复现）。原先靠 .bat 设 PYTHONIOENCODING=utf-8 绕过，
现在在 Python 内解决，不再需要任何包装脚本。
"""
import sys


def init_output(log_path=None):
    """让输出在任何平台都不因编码崩掉；log_path 非空时同时写日志文件。

    用 errors='replace' 而不是强制 encoding='utf-8'：
      前者保留平台编码，中文在 GBK 控制台下正常显示，只把 emoji 降级成 '?'；
      后者会把 UTF-8 字节写进 GBK 控制台，中文全变乱码。

    日志文件用**显式 UTF-8**，与控制台编码无关，所以不必用 shell 的 `>> x.log 2>&1`，
    三个平台行为一致。stdout 与 stderr 都会写入同一文件（等价于 shell 的 2>&1，
    保证 traceback 不会从日志里丢失）。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except (ValueError, OSError):
                pass
    if log_path:
        f = open(log_path, "a", encoding="utf-8", buffering=1)
        sys.stdout = Tee(sys.stdout, f)
        sys.stderr = Tee(sys.stderr, f)   # 共用同一句柄，等价 2>&1


class Tee:
    """同时写控制台与日志。行缓冲 + 每行 flush，长跑时日志可实时 tail。

    file 由调用方传入且**不由本类关闭**：stdout/stderr 共用同一句柄，
    谁都不该单方面关掉它。进程退出时由解释器回收。
    """

    def __init__(self, console, file):
        self.console = console
        self.file = file

    def write(self, s):
        self.console.write(s)
        self.file.write(s)
        return len(s)

    def flush(self):
        self.console.flush()
        self.file.flush()

    def isatty(self):
        """有些库据此决定是否上色/显示进度条，转发真实值避免误判"""
        return getattr(self.console, "isatty", lambda: False)()
