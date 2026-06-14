"""PyFTP server package.

模块组成：
  config       配置与运行期参数对象、用户表
  cli          命令行入口（argparse 启动参数）
  server       三种并发模型的服务器主程序
  session      会话状态与控制连接主循环
  commands     命令解析与分发表 + 所有命令处理函数
  datachannel  PORT/PASV 数据连接管理 + 限速收发
  auth         用户认证 + 登录失败锁定
  fs           文件系统操作 + 路径穿越防御 + 每用户 chroot
  throttle     令牌桶限速
  utils        日志工具
"""

__all__ = [
    "config", "cli", "server", "session", "commands",
    "datachannel", "auth", "fs", "throttle", "utils",
]
__version__ = "0.2.0"
