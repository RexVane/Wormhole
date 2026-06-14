"""
cli.py
======
命令行入口：解析启动参数，构造 ServerConfig，启动 FTPServer。

启动示例：
  python -m pyftp_server                         # 默认多线程，端口 2121
  python -m pyftp_server --model process         # 多进程模型
  python -m pyftp_server --model select          # I/O 多路复用模型
  python -m pyftp_server --port 21               # 标准端口（需 sudo）
  python -m pyftp_server --rate-limit 1048576    # 限速 1MB/s
  python -m pyftp_server --root /path/to/ftproot # 指定总根目录
"""

from __future__ import annotations
import argparse

from .config import (
    ServerConfig, CONCURRENCY_MODELS, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_ROOT,
    DEFAULT_MODEL, DEFAULT_PASV_MIN, DEFAULT_PASV_MAX, DEFAULT_RATE_LIMIT,
    DEFAULT_MAX_LOGIN_FAILS, DEFAULT_LOCK_SECONDS,
)
from .server import FTPServer
from .utils import log


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pyftp_server",
        description="课程设计 FTP 服务器（Python 实现，支持三种并发模型）")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址 (默认 {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT,
                   help=f"控制端口 (默认 {DEFAULT_PORT}；标准 FTP 为 21，需提权)")
    p.add_argument("--root", default=DEFAULT_ROOT, help="FTP 总根目录")
    p.add_argument("--model", choices=CONCURRENCY_MODELS, default=DEFAULT_MODEL,
                   help=f"并发模型: thread/process/select (默认 {DEFAULT_MODEL})")
    p.add_argument("--pasv-min", type=int, default=DEFAULT_PASV_MIN, help="被动端口下界")
    p.add_argument("--pasv-max", type=int, default=DEFAULT_PASV_MAX, help="被动端口上界")
    p.add_argument("--pasv-ip", default=None, help="对外宣告的被动模式 IP (NAT 后使用)")
    p.add_argument("--rate-limit", type=int, default=DEFAULT_RATE_LIMIT,
                   help="传输限速 (字节/秒, 0=不限)")
    p.add_argument("--max-login-fails", type=int, default=DEFAULT_MAX_LOGIN_FAILS,
                   help="登录失败锁定阈值")
    p.add_argument("--lock-seconds", type=int, default=DEFAULT_LOCK_SECONDS,
                   help="登录锁定时长 (秒)")
    p.add_argument("--quiet", action="store_true", help="关闭日志输出")
    p.add_argument("--tls", action="store_true",
                   help="启用 FTPS(显式 AUTH TLS, RFC 4217)；证书不存在时先 make cert 生成")
    p.add_argument("--tls-cert", default="certs/server.crt", help="TLS 证书路径")
    p.add_argument("--tls-key", default="certs/server.key", help="TLS 私钥路径")
    p.add_argument("--wormhole-only", action="store_true",
                   help="只保留 wormhole 频道账号(公网部署用：关闭 admin/alice 等内置弱密码账号)")
    return p


def config_from_args(args) -> ServerConfig:
    tls_cert = tls_key = None
    if args.tls:
        import os
        if not (os.path.isfile(args.tls_cert) and os.path.isfile(args.tls_key)):
            raise SystemExit(
                f"--tls 需要证书文件 {args.tls_cert} 和 {args.tls_key}\n"
                f"先生成自签证书：make cert  (或 openssl req -x509 -newkey rsa:2048 "
                f"-keyout certs/server.key -out certs/server.crt -days 365 -nodes -subj /CN=pyftp)")
        if args.model == "select":
            raise SystemExit("--tls 暂不支持 select 模型(非阻塞 TLS 握手超出课程范围)，请用 thread/process")
        tls_cert, tls_key = args.tls_cert, args.tls_key
    cfg = ServerConfig(
        host=args.host, port=args.port, root=args.root, model=args.model,
        pasv_min=args.pasv_min, pasv_max=args.pasv_max, pasv_public_ip=args.pasv_ip,
        rate_limit=args.rate_limit, max_login_fails=args.max_login_fails,
        lock_seconds=args.lock_seconds, log_enabled=not args.quiet,
        tls_cert=tls_cert, tls_key=tls_key,
    )
    if args.wormhole_only:
        # 公网部署安全加固：剔除 admin/admin 等内置弱密码账号，只留虫洞频道
        cfg.users = {"wormhole": cfg.users["wormhole"]}
    return cfg


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)
    server = FTPServer(cfg)
    try:
        server.start()
    except KeyboardInterrupt:
        log("收到中断信号，正在关闭…")
        server.stop()


if __name__ == "__main__":
    main()
