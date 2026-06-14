"""
__main__.py
===========
支持 `python -m pyftp_server` 直接启动，转发到 cli.main()。
"""
from .cli import main

if __name__ == "__main__":
    main()
