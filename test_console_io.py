"""console_io 的测试。

这里覆盖的是原先靠 .bat 设 PYTHONIOENCODING 才能绕过的问题：
Windows 中文系统重定向用 GBK，脚本里的 emoji 编不出来会崩在第一行 print。
"""
import io

import console_io


def test_init_output_survives_gbk_stream():
    """GBK 流写 emoji 原本抛 UnicodeEncodeError；处理后中文完好、emoji 降级为 ?、不崩"""
    import sys
    buf = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = buf
    try:
        console_io.init_output()
        print("🌐 出口：直连 ⚠️ 完成")
        buf.flush()
        raw = buf.buffer.getvalue()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    text = raw.decode("gbk")
    assert "出口：直连" in text        # 中文没丢
    assert "?" in text                  # emoji 被替换而非抛异常


def test_gbk_without_init_would_crash():
    """确认前一个测试防的是真问题：不处理时 GBK 流写 emoji 确实会崩"""
    buf = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    try:
        buf.write("🌐")
        buf.flush()
        raised = False
    except UnicodeEncodeError:
        raised = True
    assert raised


def test_tee_writes_both_console_and_utf8_file(tmp_path):
    """--log 由脚本以 UTF-8 写，不依赖 shell 重定向和控制台编码"""
    p = tmp_path / "t.log"
    console = io.StringIO()
    with open(p, "a", encoding="utf-8", buffering=1) as f:
        tee = console_io.Tee(console, f)
        tee.write("🌐 中文 emoji 测试\n")
        tee.flush()
    assert console.getvalue() == "🌐 中文 emoji 测试\n"
    assert p.read_text(encoding="utf-8") == "🌐 中文 emoji 测试\n"


def test_tee_write_returns_length():
    """write 要返回写入长度，否则某些调用方(如 print 内部)会误判"""
    tee = console_io.Tee(io.StringIO(), io.StringIO())
    assert tee.write("abc") == 3


def test_tee_isatty_forwards_console():
    """有些库据 isatty 决定上色/进度条，必须转发真实值"""
    class FakeTTY(io.StringIO):
        def isatty(self):
            return True
    assert console_io.Tee(FakeTTY(), io.StringIO()).isatty() is True
    assert console_io.Tee(io.StringIO(), io.StringIO()).isatty() is False


def test_init_output_tees_stdout_and_stderr_to_same_file(tmp_path):
    """等价 shell 的 2>&1：traceback 不能从日志里丢失"""
    import sys
    p = tmp_path / "t.log"
    old_out, old_err = sys.stdout, sys.stderr
    try:
        console_io.init_output(str(p))
        print("正常输出")
        print("错误输出", file=sys.stderr)
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    text = p.read_text(encoding="utf-8")
    assert "正常输出" in text and "错误输出" in text


def test_init_output_appends(tmp_path):
    """重跑要接着写，不能截断历史日志"""
    import sys
    p = tmp_path / "t.log"
    p.write_text("旧内容\n", encoding="utf-8")
    old_out, old_err = sys.stdout, sys.stderr
    try:
        console_io.init_output(str(p))
        print("新内容")
        sys.stdout.flush()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    assert p.read_text(encoding="utf-8") == "旧内容\n新内容\n"
