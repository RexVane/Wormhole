"""
session.py
==========
会话层：FTPSession 持有「单个客户端连接」的全部状态，并驱动控制连接主循环。

关键数据结构 —— 会话状态 Session：
  conn/addr     控制连接 socket 与客户端地址
  authed/user   是否已认证、当前登录用户账户
  cwd           当前虚拟工作目录（相对该用户根目录，以 / 开头）
  binary        传输类型（True=二进制 I，False=ASCII A）
  data          DataChannel 实例（PORT/PASV 数据连接管理）
  rest          断点续传偏移（REST 命令设置，RETR/STOR 消费）
  rnfr          RNFR 暂存的源路径（等待 RNTO 完成重命名）
  fs            FileSystem 实例（绑定该用户根目录，天然 chroot 隔离）

主循环 handle()：发送欢迎 -> 逐行读命令 -> 交给 commands.dispatch 分发 ->
直到 QUIT 或连接断开。单条命令异常不拖垮整个会话。
"""

from __future__ import annotations
import socket

from .config import ServerConfig, ENCODING, MAX_CMD_LEN
from .auth import Authenticator
from .datachannel import DataChannel
from . import commands
from .utils import log


class FTPSession:
    def __init__(self, conn: socket.socket, addr, cfg: ServerConfig, auth: Authenticator):
        self.conn = conn
        self.addr = addr
        self.cfg = cfg
        self.auth = auth
        # ---- 会话状态 ----
        self.authed = False
        self.pending_user: str | None = None   # USER 之后、PASS 之前暂存的用户名
        self.account = None                     # 认证通过后的 UserAccount
        self.cwd = "/"
        self.binary = True
        self.data = DataChannel(cfg)
        self.rest = 0
        self.rnfr: str | None = None            # RNFR 暂存源真实路径
        self.fs = None                          # 登录后绑定该用户的 FileSystem
        self.alive = True
        self.secure = False                     # 控制连接是否已升级 TLS(AUTH TLS)
        self.prot_p = False                     # 数据连接是否加密(PROT P)
        self._fp = None                         # 控制连接行读取器(TLS 升级后需重建)
        self._recv_buf = b""                    # select 模型：控制连接行缓冲

    # ---------- 控制连接读写 ----------
    def send(self, code: int, text: str) -> None:
        line = f"{code} {text}\r\n"
        try:
            self.conn.sendall(line.encode(ENCODING))
            log(f"-> {self.addr[0]}: {code} {text}")
        except OSError:
            self.alive = False

    def send_multi(self, code: int, lines: list[str], end_text: str) -> None:
        """多行响应（如 FEAT）：code-行... 最后 code 行。"""
        buf = "".join(f"{code}-{ln}\r\n" for ln in lines) + f"{code} {end_text}\r\n"
        try:
            self.conn.sendall(buf.encode(ENCODING))
        except OSError:
            self.alive = False

    def _readline(self, fp) -> str | None:
        try:
            raw = fp.readline(MAX_CMD_LEN)
        except OSError:
            return None
        if not raw:
            return None
        return raw.decode(ENCODING, errors="ignore").strip("\r\n")

    # ---------- 主循环（阻塞式，供 thread / process 模型使用） ----------
    def handle(self) -> None:
        log(f"客户端连接: {self.addr[0]}:{self.addr[1]}")
        self.send(220, "Welcome to PyFTP server (course design)")
        self._fp = self.conn.makefile("rb")
        try:
            while self.alive:
                line = self._readline(self._fp)
                if line is None:
                    break
                if not line:
                    continue
                self.process_command(line)
        finally:
            self.close()

    # ---------- TLS 升级（AUTH TLS，RFC 4217） ----------
    def start_tls(self, ctx) -> bool:
        """把控制连接升级为 TLS。必须在发送 234 响应之后调用：
        客户端收到 234 才发起 TLS 握手，顺序颠倒会死锁。"""
        import ssl
        try:
            self.conn = ctx.wrap_socket(self.conn, server_side=True)
            self._fp = self.conn.makefile("rb")   # 旧读取器绑定明文 socket，必须重建
            self.secure = True
            log(f"{self.addr[0]}: 控制连接已升级 TLS ({self.conn.version()})")
            return True
        except (ssl.SSLError, OSError) as e:
            log(f"{self.addr[0]}: TLS 握手失败: {e}")
            self.alive = False
            return False

    # ---------- 单条命令处理（thread/process/select 三种模型共用） ----------
    def process_command(self, line: str) -> None:
        """解析一行命令并交给分发表。三种并发模型都复用此方法。"""
        parts = line.split(" ", 1)
        cmd = parts[0].upper()
        arg = parts[1] if len(parts) > 1 else ""
        log(f"<- {self.addr[0]}: {cmd} {'****' if cmd == 'PASS' else arg}")
        commands.dispatch(self, cmd, arg)

    def send_welcome(self) -> None:
        """select 模型在连接建立时手动发送欢迎语。"""
        log(f"客户端连接: {self.addr[0]}:{self.addr[1]}")
        self.send(220, "Welcome to PyFTP server (course design)")

    def feed(self, data: bytes) -> None:
        """select 模型：喂入一段非阻塞读到的字节，按行切分后逐条处理。"""
        self._recv_buf += data
        while b"\n" in self._recv_buf and self.alive:
            raw, self._recv_buf = self._recv_buf.split(b"\n", 1)
            line = raw.decode(ENCODING, errors="ignore").strip("\r\n")
            if line:
                self.process_command(line)

    def close(self) -> None:
        self.data.close()
        try:
            self.conn.close()
        except OSError:
            pass
        log(f"客户端断开: {self.addr[0]}:{self.addr[1]}")
