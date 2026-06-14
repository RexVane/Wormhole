"""
test_wormhole.py
================
虫洞同步引擎端到端测试：
  1. 启动一个 FTP 服务器(带 wormhole 共享频道)。
  2. 创建两个 WormholeSync 实例，模拟两台电脑 A、B，各自不同的收件箱。
  3. A.send_file() 一个文件，验证 B 轮询后能自动下载到 B 的收件箱，内容一致。
  4. 验证双向：B 再发一个，A 能收到。
  5. 验证去重：再次轮询不会重复下载。
  6. 验证无死循环：B 收到的文件不会被 B 当成新文件再传回频道。

运行：python tests/test_wormhole.py
"""
from __future__ import annotations
import os
import time
import shutil
import tempfile
import threading

from pyftp_server.config import ServerConfig
from pyftp_server.server import FTPServer
from pyftp_server.wormhole.sync import WormholeSync, WormholeConfig

PORT = 2123
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


def start_server(root):
    cfg = ServerConfig(port=PORT, model="thread", root=root, log_enabled=False)
    srv = FTPServer(cfg)
    threading.Thread(target=srv.start, daemon=True).start()
    time.sleep(0.6)
    return srv


def make_sync(inbox, peer, secret=""):
    cfg = WormholeConfig(host="127.0.0.1", port=PORT, user="wormhole",
                         password="wormhole", inbox=inbox, interval=0.5, peer_id=peer,
                         secret=secret)
    return WormholeSync(cfg)


def main():
    work = tempfile.mkdtemp(prefix="wormhole_test_")
    server_root = os.path.join(work, "ftproot")
    os.makedirs(os.path.join(server_root, "wormhole"), exist_ok=True)
    inbox_a = os.path.join(work, "inbox_A")
    inbox_b = os.path.join(work, "inbox_B")
    outbox = os.path.join(work, "outbox")
    os.makedirs(outbox, exist_ok=True)

    start_server(server_root)
    a = make_sync(inbox_a, "PC-A")
    b = make_sync(inbox_b, "PC-B")

    print("[1] A 发送文件 -> B 自动收到")
    content = b"hello wormhole " + str(time.time()).encode()
    src = os.path.join(outbox, "note.txt")
    with open(src, "wb") as f:
        f.write(content)
    ok_send = a.send_file(src)
    check("A 上传成功", ok_send)
    # B 轮询拉取
    got = []
    for _ in range(10):
        got = b.poll_once()
        if got:
            break
        time.sleep(0.3)
    recv_path = os.path.join(inbox_b, "note.txt")
    check("B 收件箱出现 note.txt", os.path.isfile(recv_path))
    check("B 收到内容与 A 发送一致",
          os.path.isfile(recv_path) and open(recv_path, "rb").read() == content)
    check("阅后即焚: B 收取后服务器中转副本已删除",
          not os.path.isfile(os.path.join(server_root, "wormhole", "note.txt")))

    print("[2] 去重：B 再轮询不重复下载")
    again = b.poll_once()
    check("第二次轮询无新文件(去重生效)", again == [])

    print("[3] 无死循环：B 不会把收到的文件再传回频道")
    # B 再轮询若把刚收到的文件当新文件上传，A 就会收到一个 note.txt；A 应收不到
    a_got = []
    for _ in range(4):
        a_got = a.poll_once()
        if a_got:
            break
        time.sleep(0.3)
    # A 自己发过 note.txt 已在 seen，且 B 不该重传，故 A 不应下载到任何东西
    check("A 没有把自己发的文件又下载回来(无回环)",
          not os.path.isfile(os.path.join(inbox_a, "note.txt")))

    print("[4] 双向：B 发送 -> A 自动收到")
    content2 = b"reply from B " + str(time.time()).encode()
    src2 = os.path.join(outbox, "reply.bin")
    with open(src2, "wb") as f:
        f.write(content2)
    check("B 上传成功", b.send_file(src2))
    a_got = []
    for _ in range(10):
        a_got = a.poll_once()
        if a_got:
            break
        time.sleep(0.3)
    recv2 = os.path.join(inbox_a, "reply.bin")
    check("A 收件箱出现 reply.bin", os.path.isfile(recv2))
    check("A 收到内容与 B 发送一致",
          os.path.isfile(recv2) and open(recv2, "rb").read() == content2)

    print("[5] 回调钩子：on_received 被触发")
    fired = []
    c = make_sync(os.path.join(work, "inbox_C"), "PC-C")
    c.on_received = lambda p: fired.append(p)
    src3 = os.path.join(outbox, "ping.dat")
    with open(src3, "wb") as f:
        f.write(b"ping")
    a.send_file(src3)
    for _ in range(10):
        if c.poll_once():
            break
        time.sleep(0.3)
    check("on_received 回调被调用(供桌宠播放喷出动画)", len(fired) >= 1)

    print("[6] 端到端加密(AES-256-GCM, 服务器只见密文)")
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        has_crypto = True
    except ImportError:
        has_crypto = False
        print("  - 跳过(未安装 cryptography)")
    if has_crypto:
        d = make_sync(os.path.join(work, "inbox_D"), "PC-D", secret="虫洞口令123")
        e = make_sync(os.path.join(work, "inbox_E"), "PC-E", secret="虫洞口令123")
        secret_content = b"top secret \xe6\x9c\xba\xe5\xaf\x86 " + str(time.time()).encode()
        src_s = os.path.join(outbox, "secret.bin")
        with open(src_s, "wb") as f:
            f.write(secret_content)
        check("D 加密上传成功", d.send_file(src_s))
        # 趁 E 还没收(阅后即焚会删)，检查服务器上的中转副本是密文
        chan = os.path.join(server_root, "wormhole", "secret.bin")
        raw = open(chan, "rb").read() if os.path.isfile(chan) else b""
        check("服务器中转副本是密文(WHE1头且不含明文)",
              raw.startswith(b"WHE1") and secret_content not in raw)
        got_e = []
        for _ in range(10):
            got_e = e.poll_once()
            if got_e:
                break
            time.sleep(0.3)
        recv_s = os.path.join(work, "inbox_E", "secret.bin")
        check("E 收到并自动解密，内容与明文一致",
              os.path.isfile(recv_s) and open(recv_s, "rb").read() == secret_content)
        d.stop(); e.stop()

    a.stop(); b.stop(); c.stop()
    shutil.rmtree(work, ignore_errors=True)

    print(f"\n结果: {PASS_N} 通过, {FAIL_N} 失败")
    if FAIL_N == 0:
        print("虫洞同步引擎全部测试通过 ✓")
    else:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
