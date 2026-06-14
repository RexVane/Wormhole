"""
auth.py
=======
用户认证 + 登录失败次数限制与锁定（安全加固）。

- Authenticator 持有用户表，校验用户名/密码。
- 记录每个 IP 的连续登录失败次数；超过阈值则在 lock_seconds 内锁定该 IP，
  期间即使密码正确也拒绝，缓解暴力破解。多线程/多进程下用锁保护计数表。
- 登录成功后清零该 IP 的失败计数。

注意：多进程模型(fork)下，子进程各自持有计数表副本，锁定状态不跨进程共享；
这一限制会在报告/README 中说明（线程与 select 模型下锁定是全局生效的）。
"""

from __future__ import annotations
import threading
import time

from .config import ServerConfig, UserAccount


class Authenticator:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg
        self.users = cfg.users
        self._fails: dict[str, int] = {}        # ip -> 连续失败次数
        self._locked_until: dict[str, float] = {}  # ip -> 锁定到期时间戳
        self._lock = threading.Lock()

    # ---------- 锁定检查 ----------
    def is_locked(self, ip: str) -> tuple[bool, int]:
        """返回 (是否锁定, 剩余秒数)。"""
        with self._lock:
            until = self._locked_until.get(ip, 0)
            now = time.monotonic()
            if until > now:
                return True, int(until - now) + 1
            return False, 0

    # ---------- 凭据校验 ----------
    def check(self, ip: str, username: str, password: str) -> tuple[bool, UserAccount | None]:
        """校验用户名/密码。返回 (是否通过, 用户账户)。

        失败会累加该 IP 的失败计数并可能触发锁定；成功则清零计数。
        """
        acct = self.users.get(username)
        # 匿名账户：用户名匹配即放行（密码通常填邮箱，任意）
        ok = acct is not None and (acct.anonymous or password == acct.password)
        with self._lock:
            if ok:
                self._fails.pop(ip, None)
                self._locked_until.pop(ip, None)
                return True, acct
            # 失败：累加计数，达阈值则锁定
            n = self._fails.get(ip, 0) + 1
            self._fails[ip] = n
            if n >= self.cfg.max_login_fails:
                self._locked_until[ip] = time.monotonic() + self.cfg.lock_seconds
                self._fails[ip] = 0
            return False, None

    def user_exists(self, username: str) -> bool:
        return username in self.users
