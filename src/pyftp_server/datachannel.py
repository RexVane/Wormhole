"""
datachannel.py
==============
数据连接管理：按 RFC 959，控制连接(命令)与数据连接(文件/列表)分离。
本模块封装主动(PORT)与被动(PASV)两种模式下数据 socket 的建立、收发与限速。

- 被动 PASV：服务器在 pasv 端口范围内开一个监听 socket，把 (ip,port) 告诉
  客户端，由客户端主动连入。
- 主动 PORT：客户端把自己监听的 (ip,port) 告诉服务器，服务器主动去连。

收发数据时通过 Throttle 实现可选限速。
"""

from __future__ import annotations
import socket

from .config import ServerConfig, BUFFER_SIZE
from .throttle import Throttle


class DataChannel:
    """管理一次会话的数据连接模式与端口。"""

    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.pasv_sock: socket.socket | None = None   # 被动模式监听 socket
        self.port_addr: tuple[str, int] | None = None # 主动模式目标地址

    # ---------- 被动模式 ----------
    def open_passive(self, control_local_ip: str) -> tuple[str, int] | None:
        """在端口范围内开一个监听 socket。返回宣告给客户端的 (ip, port)。"""
        self.close()
        for port in range(self.cfg.pasv_min, self.cfg.pasv_max + 1):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((self.cfg.host, port))
                s.listen(1)
            except OSError:
                s.close()
                continue
            self.pasv_sock = s
            break
        if self.pasv_sock is None:
            return None
        real_port = self.pasv_sock.getsockname()[1]
        # 宣告 IP：优先配置的公网/局域网 IP，否则用控制连接的本地地址
        ip = self.cfg.pasv_public_ip or control_local_ip
        if ip == "0.0.0.0":
            ip = "127.0.0.1"
        return ip, real_port

    # ---------- 主动模式 ----------
    def set_port(self, ip: str, port: int) -> None:
        self.close()
        self.port_addr = (ip, port)

    # ---------- 建立数据连接 ----------
    def connect(self) -> socket.socket | None:
        """根据当前模式建立数据连接，返回数据 socket。"""
        if self.pasv_sock is not None:
            try:
                self.pasv_sock.settimeout(30)
                ds, _ = self.pasv_sock.accept()
                return ds
            except OSError:
                return None
            finally:
                self._close_pasv()
        elif self.port_addr is not None:
            try:
                ds = socket.create_connection(self.port_addr, timeout=30)
                return ds
            except OSError:
                return None
            finally:
                self.port_addr = None
        return None

    # ---------- 清理 ----------
    def _close_pasv(self) -> None:
        if self.pasv_sock is not None:
            try:
                self.pasv_sock.close()
            except OSError:
                pass
            self.pasv_sock = None

    def close(self) -> None:
        self._close_pasv()
        self.port_addr = None

    @property
    def armed(self) -> bool:
        """是否已设置好数据连接模式（PASV 监听中 或 PORT 已登记）。"""
        return self.pasv_sock is not None or self.port_addr is not None


# ---------- 限速收发：供 RETR/STOR 复用 ----------
def send_all(ds: socket.socket, fileobj, rate_limit: int) -> int:
    """从文件对象读、往数据 socket 写，带限速。返回发送字节数。"""
    throttle = Throttle(rate_limit)
    total = 0
    while True:
        chunk = fileobj.read(BUFFER_SIZE)
        if not chunk:
            break
        ds.sendall(chunk)
        throttle.consume(len(chunk))
        total += len(chunk)
    return total


def recv_all(ds: socket.socket, fileobj, rate_limit: int) -> int:
    """从数据 socket 读、往文件对象写，带限速。返回接收字节数。"""
    throttle = Throttle(rate_limit)
    total = 0
    while True:
        chunk = ds.recv(BUFFER_SIZE)
        if not chunk:
            break
        fileobj.write(chunk)
        throttle.consume(len(chunk))
        total += len(chunk)
    return total
