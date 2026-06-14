"""
commands.py
===========
命令解析与分发。所有 FTP 命令的处理函数集中在此，配合分发表 dispatch()。

关键数据结构 —— 命令分发表 COMMANDS：
  形如 {"USER": cmd_user, "RETR": cmd_retr, ...}，由命令名映射到处理函数。
  dispatch() 据此查表调用，避免一长串 if/elif，新增命令只需写函数并登记。

命令清单（基础 + 增强）：
  登录认证   USER PASS QUIT
  基础协商   SYST FEAT TYPE OPTS NOOP
  目录浏览   PWD CWD CDUP MKD RMD
  文件管理   DELE RNFR RNTO SIZE
  数据连接   PORT PASV
  目录列表   LIST NLST
  传输       RETR(下载) STOR(上传) REST(断点续传偏移)

每个处理函数签名统一为 handler(session, arg)。需要认证的命令在 dispatch()
中统一拦截：未登录时除登录/协商类命令外一律 530。
"""

from __future__ import annotations
import os

from .config import ENCODING
from .fs import FileSystem
from .datachannel import send_all, recv_all
from .utils import log

# 无需登录即可执行的命令(AUTH/PBSZ/PROT 是 TLS 协商，发生在登录之前)
_NO_AUTH = {"USER", "PASS", "QUIT", "SYST", "FEAT", "NOOP", "OPTS",
            "AUTH", "PBSZ", "PROT"}


def dispatch(session, cmd: str, arg: str) -> None:
    """查命令分发表并调用；统一处理"未实现"与"未登录"。"""
    handler = COMMANDS.get(cmd)
    if handler is None:
        session.send(502, f"Command '{cmd}' not implemented")
        return
    if cmd not in _NO_AUTH and not session.authed:
        session.send(530, "Please login with USER and PASS")
        return
    try:
        handler(session, arg)
    except Exception as e:                       # 单条命令异常不拖垮会话
        log(f"命令 {cmd} 处理异常: {e}")
        session.send(451, "Requested action aborted: local error")


# ===================== 登录认证 =====================
def cmd_user(s, arg: str) -> None:
    s.pending_user = arg.strip()
    s.authed = False
    # 不暴露"用户是否存在"，统一要求输入密码
    if s.pending_user == "anonymous":
        s.send(331, "Anonymous login ok, send your email as password")
    else:
        s.send(331, f"Password required for {s.pending_user}")


def cmd_pass(s, arg: str) -> None:
    if s.pending_user is None:
        s.send(503, "Login with USER first")
        return
    ip = s.addr[0]
    # 登录锁定检查（安全加固）
    locked, remain = s.auth.is_locked(ip)
    if locked:
        s.send(530, f"Too many failed attempts, locked for {remain}s")
        return
    ok, acct = s.auth.check(ip, s.pending_user, arg)
    if ok:
        s.authed = True
        s.account = acct
        s.cwd = "/"
        # 绑定该用户的 chroot 根目录：总根 / 用户 home
        user_root = os.path.join(s.cfg.root_abs, acct.home) if acct.home else s.cfg.root_abs
        s.fs = FileSystem(user_root)
        s.send(230, f"User {s.pending_user} logged in")
    else:
        s.authed = False
        s.send(530, "Login incorrect")


def cmd_quit(s, arg: str) -> None:
    s.send(221, "Goodbye")
    s.alive = False


# ===================== 基础协商 =====================
def cmd_syst(s, arg: str) -> None:
    s.send(215, "UNIX Type: L8")          # 宣告类 Unix，客户端按 ls 风格解析列表


def cmd_feat(s, arg: str) -> None:
    feats = ["UTF8", "PASV", "REST STREAM", "SIZE", "TYPE A;I"]
    if s.cfg.ssl_context() is not None:
        feats += ["AUTH TLS", "PBSZ", "PROT"]
    s.send_multi(211, feats, "End")


def cmd_noop(s, arg: str) -> None:
    s.send(200, "OK")


def cmd_type(s, arg: str) -> None:
    t = arg.strip().upper()
    if t.startswith("I"):
        s.binary = True
        s.send(200, "Type set to I (binary)")
    elif t.startswith("A"):
        s.binary = False
        s.send(200, "Type set to A (ASCII)")
    else:
        s.send(504, "Type not supported")


def cmd_opts(s, arg: str) -> None:
    if arg.strip().upper().startswith("UTF8"):
        s.send(200, "UTF8 set to on")
    else:
        s.send(200, "OK")


# ===================== FTPS 显式 TLS（RFC 4217） =====================
def cmd_auth(s, arg: str) -> None:
    """AUTH TLS：把控制连接升级为加密通道。流程：回 234 -> TLS 握手 -> 之后
    所有命令都走密文。客户端通常接着发 PBSZ 0 + PROT P 把数据连接也加密。"""
    if arg.strip().upper() not in ("TLS", "SSL"):
        s.send(504, "Only AUTH TLS supported")
        return
    if s.secure:
        s.send(534, "Control channel already secured")
        return
    ctx = s.cfg.ssl_context()
    if ctx is None:
        s.send(534, "TLS not configured (start server with --tls)")
        return
    if s.cfg.model == "select":
        # select 模型用非阻塞 socket，TLS 握手需特殊处理，课程设计范围内不支持
        s.send(534, "TLS unavailable in select model, use thread/process")
        return
    s.send(234, "AUTH TLS successful, starting handshake")
    s.start_tls(ctx)


def cmd_pbsz(s, arg: str) -> None:
    """保护缓冲区大小：TLS 下固定为 0(流式)，按 RFC 4217 应答即可。"""
    s.send(200, "PBSZ=0")


def cmd_prot(s, arg: str) -> None:
    """数据连接保护级别：P=加密(TLS)，C=明文。"""
    if not s.secure:
        s.send(503, "AUTH TLS first")
        return
    level = arg.strip().upper()
    if level == "P":
        s.prot_p = True
        s.send(200, "Protection level set to P (private)")
    elif level == "C":
        s.prot_p = False
        s.send(200, "Protection level set to C (clear)")
    else:
        s.send(504, "Only PROT P or C supported")


def _secure_data(s, ds):
    """PROT P 时把数据连接包成 TLS。必须在发送 150 之后调用：
    客户端收到 150 才对数据连接发起 TLS 握手，先包会死锁。
    返回包好的 socket；握手失败返回 None(原 socket 已关闭)。"""
    if not s.prot_p:
        return ds
    try:
        return s.cfg.ssl_context().wrap_socket(ds, server_side=True)
    except OSError as e:                      # ssl.SSLError 是 OSError 子类
        log(f"数据连接 TLS 握手失败: {e}")
        try:
            ds.close()
        except OSError:
            pass
        return None


def _close_data(ds) -> None:
    """关闭数据连接。TLS 连接先 unwrap 做优雅关闭(互发 close_notify)，
    否则 ftplib 客户端在传输尾部 unwrap 时会收到 EOF 报错。"""
    import ssl
    if isinstance(ds, ssl.SSLSocket):
        try:
            ds = ds.unwrap()
        except OSError:
            pass
    try:
        ds.close()
    except OSError:
        pass


# ===================== 目录浏览 =====================
def cmd_pwd(s, arg: str) -> None:
    s.send(257, f'"{s.cwd}" is current directory')


def cmd_cwd(s, arg: str) -> None:
    real = s.fs.to_real(s.cwd, arg)
    if real is None:
        s.send(550, "Permission denied: path outside root")
        return
    if not s.fs.isdir(real):
        s.send(550, "No such directory")
        return
    s.cwd = s.fs.to_virtual(s.cwd, arg)
    s.send(250, f"Directory changed to {s.cwd}")


def cmd_cdup(s, arg: str) -> None:
    cmd_cwd(s, "..")


def cmd_mkd(s, arg: str) -> None:
    if not s.account.writable:
        s.send(550, "Permission denied: read-only account")
        return
    real = s.fs.to_real(s.cwd, arg)
    if real is None:
        s.send(550, "Permission denied")
        return
    try:
        s.fs.mkdir(real)
        s.send(257, f'"{s.fs.to_virtual(s.cwd, arg)}" created')
    except FileExistsError:
        s.send(550, "Directory already exists")
    except OSError:
        s.send(550, "Create directory failed")


def cmd_rmd(s, arg: str) -> None:
    if not s.account.writable:
        s.send(550, "Permission denied: read-only account")
        return
    real = s.fs.to_real(s.cwd, arg)
    if real is None or not s.fs.isdir(real):
        s.send(550, "No such directory")
        return
    try:
        s.fs.rmdir(real)
        s.send(250, "Directory removed")
    except OSError:
        s.send(550, "Remove failed (not empty?)")


# ===================== 文件管理 =====================
def cmd_dele(s, arg: str) -> None:
    if not s.account.writable:
        s.send(550, "Permission denied: read-only account")
        return
    real = s.fs.to_real(s.cwd, arg)
    if real is None or not s.fs.isfile(real):
        s.send(550, "No such file")
        return
    try:
        s.fs.remove(real)
        s.send(250, "File deleted")
    except OSError:
        s.send(550, "Delete failed")


def cmd_rnfr(s, arg: str) -> None:
    """重命名第一步：记录源路径，等待 RNTO。"""
    real = s.fs.to_real(s.cwd, arg)
    if real is None or not s.fs.exists(real):
        s.send(550, "No such file or directory")
        return
    s.rnfr = real
    s.send(350, "Ready for RNTO")


def cmd_rnto(s, arg: str) -> None:
    """重命名第二步：把 RNFR 记录的源改名为目标。"""
    if s.rnfr is None:
        s.send(503, "RNFR required first")
        return
    if not s.account.writable:
        s.send(550, "Permission denied: read-only account")
        s.rnfr = None
        return
    dst = s.fs.to_real(s.cwd, arg)
    if dst is None:
        s.send(550, "Permission denied")
        s.rnfr = None
        return
    try:
        s.fs.rename(s.rnfr, dst)
        s.send(250, "Rename successful")
    except OSError:
        s.send(550, "Rename failed")
    finally:
        s.rnfr = None


def cmd_size(s, arg: str) -> None:
    real = s.fs.to_real(s.cwd, arg)
    if real is None or not s.fs.isfile(real):
        s.send(550, "No such file")
        return
    s.send(213, str(s.fs.size(real)))


def cmd_rest(s, arg: str) -> None:
    try:
        s.rest = int(arg)
        s.send(350, f"Restarting at {s.rest}")
    except ValueError:
        s.send(501, "Bad REST value")


# ===================== 数据连接建立 =====================
def cmd_port(s, arg: str) -> None:
    """主动模式：客户端告知它监听的 ip,port（h1,h2,h3,h4,p1,p2）。"""
    try:
        nums = [int(x) for x in arg.split(",")]
        if len(nums) != 6:
            raise ValueError
        ip = ".".join(str(n) for n in nums[:4])
        port = nums[4] * 256 + nums[5]
    except (ValueError, IndexError):
        s.send(501, "Bad PORT command")
        return
    s.data.set_port(ip, port)
    s.send(200, "PORT command successful")


def cmd_pasv(s, arg: str) -> None:
    """被动模式：服务器开监听端口，告诉客户端来连。"""
    local_ip = s.conn.getsockname()[0]
    result = s.data.open_passive(local_ip)
    if result is None:
        s.send(425, "Cannot open passive port")
        return
    ip, port = result
    h = ip.split(".")
    p1, p2 = port // 256, port % 256
    s.send(227, f"Entering Passive Mode ({h[0]},{h[1]},{h[2]},{h[3]},{p1},{p2})")


# ===================== 目录列表 =====================
def cmd_list(s, arg: str) -> None:
    _do_list(s, arg, names_only=False)


def cmd_nlst(s, arg: str) -> None:
    _do_list(s, arg, names_only=True)


def _do_list(s, arg: str, names_only: bool) -> None:
    # LIST 可能带客户端选项（如 -la），忽略以 - 开头的参数
    if arg.startswith("-"):
        arg = arg.split(" ", 1)[1] if " " in arg else ""
    real = s.fs.to_real(s.cwd, arg)
    if real is None or not s.fs.exists(real):
        s.send(550, "No such file or directory")
        return
    ds = s.data.connect()
    if ds is None:
        s.send(425, "Cannot open data connection")
        return
    s.send(150, "Opening data connection for directory list")
    ds = _secure_data(s, ds)
    if ds is None:
        s.send(426, "TLS handshake on data connection failed")
        return
    try:
        if s.fs.isdir(real):
            entries = s.fs.listdir(real)
            base = real
        else:
            entries = [os.path.basename(real)]
            base = os.path.dirname(real)
        lines = []
        for name in entries:
            if names_only:
                lines.append(name)
            else:
                lines.append(s.fs.list_line(os.path.join(base, name), name))
        payload = ("\r\n".join(l for l in lines if l) + "\r\n").encode(ENCODING)
        ds.sendall(payload)
        s.send(226, "Directory send OK")
    except OSError:
        s.send(426, "Connection closed; transfer aborted")
    finally:
        _close_data(ds)


# ===================== 下载 / 上传 / 断点续传 =====================
def cmd_retr(s, arg: str) -> None:
    real = s.fs.to_real(s.cwd, arg)
    if real is None or not s.fs.isfile(real):
        s.send(550, "No such file")
        s.rest = 0
        return
    ds = s.data.connect()
    if ds is None:
        s.send(425, "Cannot open data connection")
        return
    total = s.fs.size(real)
    s.send(150, f"Opening data connection for {os.path.basename(real)} ({total} bytes)")
    ds = _secure_data(s, ds)
    if ds is None:
        s.send(426, "TLS handshake on data connection failed")
        return
    try:
        with open(real, "rb") as f:
            if s.rest:
                f.seek(s.rest)        # 断点续传：从客户端指定偏移开始发送
                s.rest = 0
            send_all(ds, f, s.cfg.rate_limit)
        s.send(226, "Transfer complete")
    except OSError:
        s.send(426, "Transfer aborted")
    finally:
        _close_data(ds)


def cmd_stor(s, arg: str) -> None:
    if not s.account.writable:
        s.send(550, "Permission denied: read-only account")
        s.rest = 0
        return
    real = s.fs.to_real(s.cwd, arg)
    if real is None:
        s.send(550, "Permission denied")
        s.rest = 0
        return
    ds = s.data.connect()
    if ds is None:
        s.send(425, "Cannot open data connection")
        return
    s.send(150, f"Ready to receive {os.path.basename(real)}")
    ds = _secure_data(s, ds)
    if ds is None:
        s.send(426, "TLS handshake on data connection failed")
        return
    try:
        # 断点续传上传：REST 不为 0 时以 r+b 定位偏移续写，否则覆盖写
        if s.rest:
            mode = "r+b" if os.path.exists(real) else "wb"
            with open(real, mode) as f:
                f.seek(s.rest)
                s.rest = 0
                recv_all(ds, f, s.cfg.rate_limit)
        else:
            with open(real, "wb") as f:
                recv_all(ds, f, s.cfg.rate_limit)
        s.send(226, "Transfer complete")
    except OSError:
        s.send(426, "Transfer aborted")
    finally:
        _close_data(ds)


# ===================== 命令分发表 =====================
# 命令名 -> 处理函数。dispatch() 查此表调用。
COMMANDS = {
    "USER": cmd_user, "PASS": cmd_pass, "QUIT": cmd_quit,
    "SYST": cmd_syst, "FEAT": cmd_feat, "NOOP": cmd_noop,
    "TYPE": cmd_type, "OPTS": cmd_opts,
    "AUTH": cmd_auth, "PBSZ": cmd_pbsz, "PROT": cmd_prot,
    "PWD": cmd_pwd, "XPWD": cmd_pwd, "CWD": cmd_cwd, "CDUP": cmd_cdup,
    "MKD": cmd_mkd, "XMKD": cmd_mkd, "RMD": cmd_rmd, "XRMD": cmd_rmd,
    "DELE": cmd_dele, "RNFR": cmd_rnfr, "RNTO": cmd_rnto, "SIZE": cmd_size,
    "PORT": cmd_port, "PASV": cmd_pasv,
    "LIST": cmd_list, "NLST": cmd_nlst,
    "RETR": cmd_retr, "STOR": cmd_stor, "REST": cmd_rest,
}
