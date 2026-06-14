"""
server.py
=========
FTP 服务器主程序，支持三种可切换的并发模型，便于做性能对比实验：

  1. thread  —— 多线程，one-thread-per-connection。每个连接一个线程，
                逻辑直观；受 GIL 影响，CPU 密集场景扩展有限，但 FTP 以
                I/O 为主，线程模型表现通常很好。
  2. process —— 多进程，每个连接 fork 一个子进程。绕开 GIL，进程间隔离，
                但创建开销与内存占用更大。（依赖 os.fork，Unix/macOS 可用）
  3. select  —— I/O 多路复用，单线程用 selectors(epoll/kqueue) 同时管理
                所有控制连接的读写事件。无线程/进程切换开销，高并发轻量；
                本实现中数据传输仍为阻塞式（课程设计范围内的简化）。

通过启动参数 --model 选择，便于在报告里做"并发数 vs 吞吐量/响应时间"对比。
"""

from __future__ import annotations
import os
import socket
import threading
import selectors

from .config import ServerConfig
from .auth import Authenticator
from .session import FTPSession
from .utils import log, set_log_enabled


class FTPServer:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.auth = Authenticator(cfg)
        self.sock: socket.socket | None = None
        self._running = False
        set_log_enabled(cfg.log_enabled)

    def _listen_socket(self) -> socket.socket:
        os.makedirs(self.cfg.root_abs, exist_ok=True)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.cfg.host, self.cfg.port))
        s.listen(64)
        log(f"FTP 服务器启动: {self.cfg.host}:{self.cfg.port}  "
            f"模型={self.cfg.model}  根目录={self.cfg.root_abs}")
        log(f"可用账号: {', '.join(self.cfg.users.keys())}")
        if self.cfg.rate_limit > 0:
            log(f"限速: {self.cfg.rate_limit} 字节/秒")
        return s

    def start(self) -> None:
        self.sock = self._listen_socket()
        self._running = True
        try:
            if self.cfg.model == "thread":
                self._serve_thread()
            elif self.cfg.model == "process":
                self._serve_process()
            elif self.cfg.model == "select":
                self._serve_select()
            else:
                raise ValueError(f"未知并发模型: {self.cfg.model}")
        finally:
            self.stop()

    # ---------- 模型一：多线程 ----------
    def _serve_thread(self) -> None:
        while self._running:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            t = threading.Thread(
                target=self._run_session, args=(conn, addr), daemon=True)
            t.start()

    # ---------- 模型二：多进程（fork） ----------
    def _serve_process(self) -> None:
        if not hasattr(os, "fork"):
            log("当前平台不支持 os.fork，多进程模型不可用，请改用 thread 或 select")
            return
        while self._running:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                break
            pid = os.fork()
            if pid == 0:
                # 子进程：关闭监听 socket 副本，独立处理该连接后退出
                try:
                    self.sock.close()
                    self._run_session(conn, addr)
                finally:
                    os._exit(0)
            else:
                # 父进程：关闭连接 socket 副本，回收已结束的子进程
                conn.close()
                self._reap_children()

    @staticmethod
    def _reap_children() -> None:
        try:
            while True:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
        except ChildProcessError:
            pass

    # ---------- 模型三：I/O 多路复用 ----------
    def _serve_select(self) -> None:
        sel = selectors.DefaultSelector()
        self.sock.setblocking(False)
        sel.register(self.sock, selectors.EVENT_READ, data=None)
        sessions: dict[socket.socket, FTPSession] = {}
        while self._running:
            events = sel.select(timeout=1)
            for key, _ in events:
                if key.data is None:
                    # 监听 socket 可读：接受新连接
                    try:
                        conn, addr = self.sock.accept()
                    except OSError:
                        continue
                    conn.setblocking(False)
                    sess = FTPSession(conn, addr, self.cfg, self.auth)
                    sess.send_welcome()
                    sessions[conn] = sess
                    sel.register(conn, selectors.EVENT_READ, data=sess)
                else:
                    # 客户端控制连接可读：读取并喂给会话处理
                    sess: FTPSession = key.data
                    conn = key.fileobj
                    try:
                        data = conn.recv(4096)
                    except BlockingIOError:
                        continue
                    except OSError:
                        data = b""
                    if not data:
                        sel.unregister(conn)
                        sessions.pop(conn, None)
                        sess.close()
                        continue
                    # 数据传输期间会临时切回阻塞，处理完恢复非阻塞
                    conn.setblocking(True)
                    sess.feed(data)
                    if sess.alive:
                        conn.setblocking(False)
                    else:
                        sel.unregister(conn)
                        sessions.pop(conn, None)
                        sess.close()
        sel.close()

    # ---------- 会话执行（thread/process 共用） ----------
    def _run_session(self, conn: socket.socket, addr) -> None:
        FTPSession(conn, addr, self.cfg, self.auth).handle()

    def stop(self) -> None:
        self._running = False
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        log("FTP 服务器已停止")
