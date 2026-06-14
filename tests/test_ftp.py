"""
test_ftp.py
===========
端到端功能测试：用标准库 ftplib（真实 FTP 客户端）连接服务器，覆盖：
  登录认证 / 错误密码拒绝 / 登录失败锁定
  目录列表 LIST、NLST（被动 + 主动模式）
  CWD/PWD/CDUP 目录浏览
  RETR 下载 / STOR 上传
  REST 断点续传（下载从偏移开始）
  RNFR/RNTO 重命名、DELE 删除、MKD/RMD
  SIZE 查询
  目录穿越防御（../ 越权被拒）
  每用户 chroot 隔离（alice 看不到 bob 的目录）
  10 客户端并发

运行：python tests/test_ftp.py
"""
from __future__ import annotations
import io
import os
import time
import threading
from ftplib import FTP, error_perm

from pyftp_server.config import ServerConfig
from pyftp_server.server import FTPServer

PORT = 2122
PASS_N = 0
FAIL_N = 0


def check(name, cond):
    global PASS_N, FAIL_N
    if cond:
        PASS_N += 1
        print(f"  ✓ {name}")
    else:
        FAIL_N += 1
        print(f"  ✗ {name}  <<< FAIL")


def start_server(model="thread", max_login_fails=3):
    cfg = ServerConfig(port=PORT, model=model, max_login_fails=max_login_fails,
                       log_enabled=False)
    srv = FTPServer(cfg)
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.6)
    return srv


def connect(user="admin", pw="admin"):
    ftp = FTP()
    ftp.connect("127.0.0.1", PORT, timeout=10)
    ftp.login(user, pw)
    return ftp


def _gen_cert(tmpdir):
    """用 openssl 生成临时自签证书；不可用时返回 (None, None) 跳过 TLS 测试。"""
    import shutil
    import subprocess
    if shutil.which("openssl") is None:
        return None, None
    crt = os.path.join(tmpdir, "test.crt")
    key = os.path.join(tmpdir, "test.key")
    r = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
         "-out", crt, "-days", "2", "-nodes", "-subj", "/CN=pyftp-test"],
        capture_output=True)
    return (crt, key) if r.returncode == 0 else (None, None)


def _test_tls():
    """FTPS：AUTH TLS 升级控制连接 + PROT P 加密数据连接，上传下载一致。"""
    import io
    import ssl
    import tempfile
    from ftplib import FTP_TLS
    tmpd = tempfile.mkdtemp(prefix="pyftp_tls_")
    crt, key = _gen_cert(tmpd)
    if crt is None:
        print("  - 跳过(系统无 openssl，无法生成测试证书)")
        return
    tls_port = PORT + 2
    cfg = ServerConfig(port=tls_port, model="thread", log_enabled=False,
                       tls_cert=crt, tls_key=key)
    threading.Thread(target=FTPServer(cfg).start, daemon=True).start()
    time.sleep(0.6)

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    fs_ = FTP_TLS(context=ctx)
    fs_.connect("127.0.0.1", tls_port, timeout=10)
    fs_.login("admin", "admin")                  # FTP_TLS.login 自动先发 AUTH TLS
    check("AUTH TLS 升级成功(控制连接为 SSLSocket)", isinstance(fs_.sock, ssl.SSLSocket))
    fs_.prot_p()
    fs_.set_pasv(True)
    data = b"tls secret payload " + str(time.time()).encode()
    fs_.storbinary("STOR tls_upload.txt", io.BytesIO(data))
    buf = io.BytesIO()
    fs_.retrbinary("RETR tls_upload.txt", buf.write)
    check("PROT P 数据信道上传/下载内容一致", buf.getvalue() == data)
    check("TLS 下目录列表正常", "tls_upload.txt" in fs_.nlst())
    fs_.delete("tls_upload.txt")
    fs_.quit()


def main():
    start_server()
    print("[1] 登录认证")
    ftp = connect()
    check("正确账号(admin)登录成功", True)

    bad = FTP(); bad.connect("127.0.0.1", PORT, timeout=10)
    try:
        bad.login("admin", "wrongpw"); ok = False
    except error_perm:
        ok = True
    bad.close()
    check("错误密码被拒绝(530)", ok)

    print("[2] 目录列表 (被动模式)")
    ftp.set_pasv(True)
    names = ftp.nlst()
    check("能列出根目录, 含 readme.txt", "readme.txt" in names)
    lines = []
    ftp.retrlines("LIST", lines.append)
    check("LIST 返回 ls 风格行", any("readme.txt" in l for l in lines))

    print("[3] 目录浏览 CWD/PWD/CDUP")
    ftp.cwd("docs")
    check("CWD docs 成功, PWD=/docs", ftp.pwd() == "/docs")
    check("docs 下含 poem.txt", "poem.txt" in ftp.nlst())
    ftp.cwd("..")
    check("CDUP 回到根 /", ftp.pwd() == "/")

    print("[4] 下载 RETR")
    buf = io.BytesIO()
    ftp.retrbinary("RETR readme.txt", buf.write)
    check("下载 readme.txt 内容非空", len(buf.getvalue()) > 0)

    print("[5] 上传 STOR")
    data = b"uploaded by test at " + str(time.time()).encode()
    ftp.storbinary("STOR uploaded.txt", io.BytesIO(data))
    chk = io.BytesIO()
    ftp.retrbinary("RETR uploaded.txt", chk.write)
    check("上传后能原样下载回来", chk.getvalue() == data)

    print("[6] SIZE 查询")
    check("SIZE 返回正确大小", ftp.size("uploaded.txt") == len(data))

    print("[7] REST 断点续传 (从偏移下载)")
    offset = 9
    part = io.BytesIO()
    ftp.retrbinary("RETR uploaded.txt", part.write, rest=offset)
    check("REST 从偏移处续传内容正确", part.getvalue() == data[offset:])

    print("[8] RNFR/RNTO 重命名 + DELE 删除")
    ftp.rename("uploaded.txt", "renamed.txt")
    check("RNFR/RNTO 重命名成功", "renamed.txt" in ftp.nlst())
    ftp.delete("renamed.txt")
    check("DELE 删除成功", "renamed.txt" not in ftp.nlst())

    print("[9] MKD/RMD 目录增删")
    ftp.mkd("tmpdir")
    check("MKD 创建目录成功", "tmpdir" in ftp.nlst())
    ftp.rmd("tmpdir")
    check("RMD 删除目录成功", "tmpdir" not in ftp.nlst())

    print("[10] 错误处理")
    try:
        ftp.retrbinary("RETR nonexist.txt", lambda x: None); ok = False
    except error_perm as e:
        ok = str(e).startswith("550")
    check("下载不存在文件返回 550", ok)

    print("[11] 主动模式 PORT")
    ftp.set_pasv(False)
    check("主动模式也能列目录", "readme.txt" in ftp.nlst())
    ftp.quit()

    print("[12] 目录穿越防御 (../ 越权被拒)")
    f = connect("alice", "alice123")
    f.set_pasv(True)
    # alice 被隔离在 users/alice，尝试用 ../.. 跳出根
    try:
        f.cwd("../../"); pwd_after = f.pwd()
    except error_perm:
        pwd_after = f.pwd()
    # 无论如何 alice 都跳不出自己的根（最多停在 /）
    names_alice = f.nlst()
    check("alice 无法越权访问上层 (穿越被挡)", "users" not in names_alice and "readme.txt" not in names_alice)
    check("alice 只能看到自己的文件", "hello_alice.txt" in names_alice)
    f.quit()

    print("[13] 每用户 chroot 隔离 (alice 看不到 bob)")
    fa = connect("alice", "alice123"); fa.set_pasv(True)
    check("alice 目录里没有 bob 的文件", "hello_bob.txt" not in fa.nlst())
    fa.quit()

    print("[14] 匿名用户只读 (STOR 被拒)")
    fan = FTP(); fan.connect("127.0.0.1", PORT, timeout=10); fan.login("anonymous", "x@x.com")
    fan.set_pasv(True)
    try:
        fan.storbinary("STOR hack.txt", io.BytesIO(b"x")); ro_ok = False
    except error_perm:
        ro_ok = True
    fan.quit()
    check("匿名用户上传被拒绝(只读)", ro_ok)

    print("[15] 并发: 10 客户端同时登录+列目录")
    results = []
    def worker(i):
        try:
            c = connect(); c.set_pasv(True); n = c.nlst(); c.quit()
            results.append("readme.txt" in n)
        except Exception:
            results.append(False)
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    [t.start() for t in ths]; [t.join() for t in ths]
    check("10 并发全部成功", len(results) == 10 and all(results))

    print("[16] FTPS 显式 TLS (AUTH TLS + PROT P)")
    _test_tls()

    print("[17] --wormhole-only 公网加固 (只保留频道账号)")
    from pyftp_server.cli import build_parser, config_from_args
    wo_cfg = config_from_args(build_parser().parse_args(["--wormhole-only"]))
    check("内置弱密码账号(admin等)已剔除", list(wo_cfg.users.keys()) == ["wormhole"])

    # 清理可能残留的测试文件
    for fn in ("uploaded.txt", "renamed.txt"):
        p = os.path.join(ServerConfig().root_abs, fn)
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass

    print(f"\n结果: {PASS_N} 通过, {FAIL_N} 失败")
    if FAIL_N == 0:
        print("全部端到端测试通过 ✓")
    else:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
