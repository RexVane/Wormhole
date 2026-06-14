"""
fs.py
=====
文件系统操作 + 路径安全（防目录穿越）。

核心职责：把"FTP 虚拟路径"映射成"真实磁盘路径"，并保证无论客户端怎么
构造路径（含 .. / 绝对路径），都不能跳出该用户的根目录(chroot)。

每个 FileSystem 实例绑定一个用户的根目录(user_root)，因此天然实现了
"每用户根目录隔离"——不同用户持有不同 FileSystem，互相看不到对方的文件。
"""

from __future__ import annotations
import os
import time


class FileSystem:
    def __init__(self, user_root: str):
        # 该用户被限制的真实根目录（已是绝对路径）
        self.root = os.path.abspath(user_root)
        os.makedirs(self.root, exist_ok=True)

    # ---------- 路径映射与安全校验 ----------
    def _normalize(self, cwd: str, arg: str) -> list[str]:
        """把 (当前虚拟目录 cwd, 客户端参数 arg) 规范化为路径段列表。"""
        arg = (arg or "").strip()
        if arg.startswith("/"):
            virtual = arg                      # 绝对虚拟路径
        else:
            virtual = (cwd.rstrip("/") + "/" + arg) if arg else cwd
        parts: list[str] = []
        for seg in virtual.split("/"):
            if seg in ("", "."):
                continue
            if seg == "..":
                if parts:
                    parts.pop()                # 回退一级；已在根则忽略，无法越权
                continue
            parts.append(seg)
        return parts

    def to_real(self, cwd: str, arg: str) -> str | None:
        """虚拟路径 -> 真实路径，越权返回 None（防目录穿越的关键校验）。"""
        parts = self._normalize(cwd, arg)
        real_abs = os.path.abspath(os.path.join(self.root, *parts))
        # 必须仍在 root 之内（等于 root 或以 root/ 为前缀）
        if real_abs != self.root and not real_abs.startswith(self.root + os.sep):
            return None
        return real_abs

    def to_virtual(self, cwd: str, arg: str) -> str:
        """虚拟路径规范化（给 CWD/PWD 用），始终以 / 开头。"""
        parts = self._normalize(cwd, arg)
        return "/" + "/".join(parts)

    # ---------- 文件/目录操作 ----------
    def isdir(self, real: str) -> bool:
        return os.path.isdir(real)

    def isfile(self, real: str) -> bool:
        return os.path.isfile(real)

    def exists(self, real: str) -> bool:
        return os.path.exists(real)

    def size(self, real: str) -> int:
        return os.path.getsize(real)

    def listdir(self, real: str) -> list[str]:
        return sorted(os.listdir(real))

    def remove(self, real: str) -> None:
        os.remove(real)

    def mkdir(self, real: str) -> None:
        os.makedirs(real, exist_ok=False)

    def rmdir(self, real: str) -> None:
        os.rmdir(real)

    def rename(self, src: str, dst: str) -> None:
        os.rename(src, dst)

    # ---------- 目录列表格式化 ----------
    def list_line(self, real_path: str, name: str) -> str:
        """生成类 Unix `ls -l` 风格的一行，供 LIST 命令使用。

        Windows 自带 ftp、FileZilla 都能解析这种格式。
        字段：权限 链接数 属主 属组 大小 月 日 时间 文件名
        """
        try:
            st = os.stat(real_path)
        except OSError:
            return ""
        is_dir = os.path.isdir(real_path)
        perm = "drwxr-xr-x" if is_dir else "-rw-r--r--"
        mtime = time.strftime("%b %d %H:%M", time.localtime(st.st_mtime))
        return f"{perm} 1 owner group {st.st_size:>12} {mtime} {name}"
