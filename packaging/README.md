# 虫洞桌宠 · 轻量 app 打包

把虫洞桌宠客户端(`src/pyftp_server/wormhole/pet.py`,PySide6 + QML)打包成
**单文件可执行**,双击即用、免装 Python。Windows 产出 `.exe`,macOS 产出 `.app`。

## 目录

| 文件 | 作用 |
|---|---|
| `wormhole-pet.spec` | PyInstaller 打包规格(跨平台,Win/.exe + Mac/.app 同一份) |
| `pet_entry.py` | 打包入口脚本(解决 `pet.py` 相对导入,使其可作顶层入口) |
| `build-windows.bat` | Windows 一键构建 |
| `build-mac.sh` | macOS 一键构建 |

## 构建

**Windows**
```bat
cd packaging
build-windows.bat
:: 产物:packaging\dist\虫洞桌宠.exe
```

**macOS**
```bash
cd packaging
bash build-mac.sh
# 产物:packaging/dist/虫洞桌宠.app
```

依赖(脚本会自动补装):`PySide6`、`cryptography`、`pyinstaller`;
macOS 上「挂件常驻所有桌面」效果另需 `pyobjc-framework-Cocoa`(可选,不装功能不受影响)。

## 运行

打包后的程序接受与 `pet.py` 完全相同的命令行参数:

```
虫洞桌宠 --host <服务器IP> --port 2121 \
        --user wormhole --password '<FTP密码>' \
        --tls --secret '<两端一致的端到端加密口令>'
```

- `--tls`:FTPS 加密连接(服务器需以 `--tls` 启动)
- `--secret`:端到端加密口令,两台设备必须一致
- `--size N`:挂件边长像素(0 = 随屏幕自适应)
- 收到的文件落在 `~/Wormhole/收件箱/`

> 直接双击不带参数时,会以默认值(`127.0.0.1:2121`、无加密)启动,仅适合本机自测。
> 实际使用建议做一个带参数的快捷方式 / 启动脚本。

## 体积说明

单文件包含整个 Qt Quick 运行时,体积约 150–170MB,属 PySide6 应用的正常范围。
spec 已排除 WebEngine、Multimedia、3D、Charts 等无用重型模块以尽量压缩。

## 原理要点

- `wormhole.qml` 作为数据文件打进包;运行时 `pet.py:_qml_path()` 优先从
  PyInstaller 解压目录 `sys._MEIPASS/pyftp_server/wormhole/` 读取,源码运行时
  回退到模块同级目录——同一份代码兼容两种环境。
- `console=False`:GUI 程序不弹命令行黑窗。
- 加密所需的 `cryptography` 由 PyInstaller 钩子自动收集进包。
