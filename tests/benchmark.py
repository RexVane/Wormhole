"""
benchmark.py
============
性能对比脚本：对三种并发模型(thread / process / select)，在不同并发客户端数下
传输同一个文件，统计总耗时、吞吐量与平均每客户端耗时，输出对比表格。

做法：
  1. 依次以每种模型启动一个服务器子进程（不同端口，避免冲突）。
  2. 在测试用根目录里生成一个固定大小的文件(默认 5MB)。
  3. 对每个并发数 N，开 N 个线程各自登录并下载该文件，记录墙钟耗时。
  4. 汇总吞吐量 = N * 文件大小 / 总耗时，打印对比表。

运行：python tests/benchmark.py
可选：python tests/benchmark.py --size-mb 10 --concurrency 1,5,10,20
"""
from __future__ import annotations
import argparse
import io
import os
import sys
import time
import socket
import threading
import subprocess
from ftplib import FTP

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
SRC = os.path.join(PROJECT, "src")
BENCH_ROOT = os.path.join(PROJECT, "examples", "ftproot")
BENCH_FILE = "benchmark_payload.bin"

MODELS = ["thread", "process", "select"]
BASE_PORT = 2200


def wait_port(port, timeout=5.0):
    """等待服务器端口可连接。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def make_payload(size_mb: int) -> int:
    path = os.path.join(BENCH_ROOT, BENCH_FILE)
    size = size_mb * 1024 * 1024
    with open(path, "wb") as f:
        f.write(os.urandom(size))
    return size


def start_server(model: str, port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = SRC
    proc = subprocess.Popen(
        [sys.executable, "-m", "pyftp_server",
         "--model", model, "--port", str(port), "--quiet"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc


def download_once(port: int) -> bool:
    try:
        ftp = FTP()
        ftp.connect("127.0.0.1", port, timeout=30)
        ftp.login("admin", "admin")
        ftp.set_pasv(True)
        sink = io.BytesIO()
        ftp.retrbinary(f"RETR {BENCH_FILE}", sink.write)
        ftp.quit()
        return True
    except Exception:
        return False


def run_concurrency(port: int, n: int) -> tuple[float, int]:
    """开 n 个线程并发下载，返回 (墙钟耗时秒, 成功数)。"""
    ok = []
    lock = threading.Lock()

    def worker():
        good = download_once(port)
        with lock:
            ok.append(good)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - t0
    return elapsed, sum(ok)


def main():
    ap = argparse.ArgumentParser(description="三种并发模型性能对比")
    ap.add_argument("--size-mb", type=int, default=5, help="测试文件大小 (MB)")
    ap.add_argument("--concurrency", default="1,5,10,20",
                    help="并发客户端数列表, 逗号分隔")
    args = ap.parse_args()

    concurrency = [int(x) for x in args.concurrency.split(",")]
    size = make_payload(args.size_mb)
    print(f"测试文件: {BENCH_FILE}  大小: {args.size_mb} MB")
    print(f"并发数: {concurrency}")
    print(f"平台 fork 支持: {hasattr(os, 'fork')}\n")

    results = {}  # model -> {n: (elapsed, throughput_MBps, ok)}
    for i, model in enumerate(MODELS):
        if model == "process" and not hasattr(os, "fork"):
            print(f"[{model}] 当前平台不支持 fork，跳过\n")
            continue
        port = BASE_PORT + i
        proc = start_server(model, port)
        if not wait_port(port):
            print(f"[{model}] 服务器启动失败，跳过")
            proc.terminate()
            continue
        print(f"=== 模型: {model} (端口 {port}) ===")
        results[model] = {}
        for n in concurrency:
            elapsed, ok = run_concurrency(port, n)
            total_mb = (size * ok) / (1024 * 1024)
            tput = total_mb / elapsed if elapsed > 0 else 0
            results[model][n] = (elapsed, tput, ok)
            print(f"  并发 {n:>3}: 耗时 {elapsed:6.3f}s  "
                  f"吞吐 {tput:7.2f} MB/s  成功 {ok}/{n}")
        proc.terminate()
        proc.wait(timeout=5)
        print()

    # 汇总对比表
    print("=" * 60)
    print("性能对比汇总 (吞吐量 MB/s, 越高越好)")
    print("=" * 60)
    header = "并发数".ljust(8) + "".join(m.ljust(14) for m in results)
    print(header)
    for n in concurrency:
        row = str(n).ljust(8)
        for model in results:
            tput = results[model].get(n, (0, 0, 0))[1]
            row += f"{tput:.2f}".ljust(14)
        print(row)

    # 清理测试文件
    try:
        os.remove(os.path.join(BENCH_ROOT, BENCH_FILE))
    except OSError:
        pass


if __name__ == "__main__":
    main()
