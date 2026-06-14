"""
utils.py
========
通用工具：线程/进程安全的日志输出。

路径安全与目录列表格式化已迁移到 fs.py（FileSystem 类），本模块只保留
与具体业务无关的日志工具，避免循环依赖。
"""

from __future__ import annotations
import os
import sys
import threading
import time

_log_lock = threading.Lock()
_log_enabled = True


def set_log_enabled(enabled: bool) -> None:
    global _log_enabled
    _log_enabled = enabled


def log(msg: str) -> None:
    """带时间戳、带进程号的日志。多线程下用锁串行化，多进程下靠 pid 区分。"""
    if not _log_enabled:
        return
    with _log_lock:
        ts = time.strftime("%H:%M:%S")
        sys.stdout.write(f"[{ts}][pid {os.getpid()}] {msg}\n")
        sys.stdout.flush()
