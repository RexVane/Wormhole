"""
config.py
=========
FTP 服务器全局配置 + 运行期配置对象。

设计要点：
- 模块级常量给出"出厂默认值"，一处集中管理所有可调参数。
- ServerConfig 是一个运行期配置对象，由 cli.py 解析启动参数后构造，
  贯穿整个服务器生命周期（监听地址/端口、并发模型、限速、登录锁定等）。
- 用户表 USERS 支持「每用户独立根目录」(chroot 风格隔离)，每个账号可绑定
  自己的 home 子目录，互相看不到对方的文件。
"""

from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path

# ===================== 出厂默认值（可被启动参数覆盖） =====================

# 控制连接监听地址与端口（标准 FTP 控制端口为 21；非 root 用户用 2121 免提权）
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 2121

# 被动模式（PASV）数据端口范围。Windows ftp 默认主动(PORT)，FileZilla 等默认被动(PASV)
DEFAULT_PASV_MIN = 50000
DEFAULT_PASV_MAX = 50100

# 并发模型：thread=多线程 / process=多进程 / select=I/O多路复用(单线程异步)
DEFAULT_MODEL = "thread"
CONCURRENCY_MODELS = ("thread", "process", "select")

# 传输限速（字节/秒）。0 表示不限速。可用于限速与拥塞观察。
DEFAULT_RATE_LIMIT = 0

# 登录失败次数限制与锁定（安全加固）
DEFAULT_MAX_LOGIN_FAILS = 5      # 同一 IP 连续失败上限
DEFAULT_LOCK_SECONDS = 60        # 超过上限后锁定时长（秒）

# 编码：控制连接命令与目录列表统一 UTF-8
ENCODING = "utf-8"

# 单条命令行最大长度（防御超长输入）
MAX_CMD_LEN = 4096

# 数据传输缓冲块大小
BUFFER_SIZE = 8192

# 项目根目录（仓库根），默认 FTP 总根目录指向 examples/ftproot
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = os.environ.get("PYFTP_ROOT", str(PROJECT_ROOT / "examples" / "ftproot"))


# ===================== 用户表（每用户 chroot 隔离） =====================
#
# 每个用户绑定：密码 + 相对总根目录的 home 子目录 + 是否可写。
# 登录后该用户被限制在 <总根>/<home> 内，无法越权访问其他用户或上层目录。
#
@dataclass
class UserAccount:
    password: str
    home: str          # 相对总根目录的子目录名；"" 表示就是总根
    writable: bool = True
    anonymous: bool = False


def default_users() -> dict[str, UserAccount]:
    """出厂用户表。home 各不相同，体现 chroot 风格隔离。"""
    return {
        # 普通用户：隔离在 users/alice 子目录，可读写
        "alice": UserAccount(password="alice123", home="users/alice", writable=True),
        "bob": UserAccount(password="bob123", home="users/bob", writable=True),
        # 管理员：根目录可读写
        "admin": UserAccount(password="admin", home="", writable=True),
        # 兼容旧测试的账号
        "user": UserAccount(password="123456", home="", writable=True),
        # 匿名：隔离在 pub 目录，只读
        "anonymous": UserAccount(password="", home="pub", writable=False, anonymous=True),
        # 虫洞文件传输共享频道：两台电脑用此账号连同一中转区收发文件
        # 公网部署时务必用环境变量 PYFTP_WORMHOLE_PASSWORD 改掉默认弱密码
        "wormhole": UserAccount(
            password=os.environ.get("PYFTP_WORMHOLE_PASSWORD", "wormhole"),
            home="wormhole", writable=True),
    }


# ===================== 运行期配置对象 =====================
@dataclass
class ServerConfig:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    root: str = DEFAULT_ROOT
    model: str = DEFAULT_MODEL
    pasv_min: int = DEFAULT_PASV_MIN
    pasv_max: int = DEFAULT_PASV_MAX
    pasv_public_ip: str | None = None         # NAT 后对外宣告的被动 IP；None=用本地连接地址
    rate_limit: int = DEFAULT_RATE_LIMIT      # 字节/秒，0=不限
    max_login_fails: int = DEFAULT_MAX_LOGIN_FAILS
    lock_seconds: int = DEFAULT_LOCK_SECONDS
    log_enabled: bool = True
    tls_cert: str | None = None               # TLS 证书路径(与 tls_key 同时配置则支持 FTPS)
    tls_key: str | None = None                # TLS 私钥路径
    users: dict[str, UserAccount] = field(default_factory=default_users)

    @property
    def root_abs(self) -> str:
        return os.path.abspath(self.root)

    def ssl_context(self):
        """懒加载并缓存服务端 SSLContext；未配置证书时返回 None(不支持 TLS)。"""
        if not (self.tls_cert and self.tls_key):
            return None
        if getattr(self, "_ssl_ctx", None) is None:
            import ssl
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(self.tls_cert, self.tls_key)
            self._ssl_ctx = ctx
        return self._ssl_ctx
